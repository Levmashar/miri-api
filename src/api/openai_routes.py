"""
OpenAI-compatible API routes.

Provides:
  POST /v1/chat/completions   — chat completions (with tool/function calling)
  GET  /v1/models             — list available models

All requests are serialized through an asyncio.Lock because the underlying
Playwright browser page is single-threaded.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from src.api.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCallInfo,
    FunctionDefinition,
    ImageData,
    ImageGenerationRequest,
    ImagesResponse,
    ImageUsage,
    ImageUsageDetails,
    ModelListResponse,
    ModelObject,
    ResponseFunctionCall,
    ResponseObject,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponsesRequest,
    ResponseUsage,
    ToolCall,
    ToolDefinition,
    UsageInfo,
    VideoData,
    VideoGenerationRequest,
    VideosResponse,
)
from src.api.media_files import media_url
from src.api.worker_pool import BrowserBusy, AccountUnavailable
from src.api.failover import retry_across_accounts
from src.providers.base import (
    ENABLED_PROVIDER_IDS,
    chat_model_catalog,
    get_provider,
    model_id_to_provider,
    resolve_chat_model,
)
from src.providers.qwen import models as qwen_models
from src.providers.seedream import models as seedream_models
from src.providers.chatgpt.client import ChatGPTClient
from src.providers.claude.client import ClaudeClient
from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("openai_routes")

openai_router = APIRouter()

# Hoisted out of per-request hot paths (compiled/allocated once at import).
_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```")
_UNSAFE_NAME_RE = re.compile(r"[^\w.\-]")
# Markers that reveal the scraper grabbed the SENT prompt instead of the reply.
_ECHO_MARKERS = ("[System instruction:", "tool-calling mode", "Available functions:")

# Concurrency lives in the shared MultiAccountPool. A request checks out one
# worker (its own tab of one account) for its whole duration; tabs never mix and
# never cross accounts. This router and the legacy router share the one pool.
_pool = None
_usage = None


# ── Responses API conversation chaining ─────────────────────────
#
# previous_response_id continues a conversation. A /c/<id> thread's cookies live
# in ONE account's profile, so a continuation MUST run on that SAME account — no
# other account can open that thread. We therefore map
#   response_id -> (account_id, thread_id)
# and, on a chained request, do a PINNED acquire from that account only. If the
# account is unavailable we 409 — we NEVER reroute a chain to another account
# (that would silently answer with no context, or worse, in someone else's tab).
_response_threads: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
_MAX_TRACKED_RESPONSES = 500

# Per-conversation lock: two concurrent continuations of the SAME thread must
# not drive that one /c/<id> from two tabs at once. Serialize them.
_thread_locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()
_MAX_THREAD_LOCKS = 500


def _remember_response_thread(response_id: str, account_id: str, thread_id: str) -> None:
    if not response_id or not account_id or not thread_id:
        return
    _response_threads[response_id] = (account_id, thread_id)
    _response_threads.move_to_end(response_id)
    while len(_response_threads) > _MAX_TRACKED_RESPONSES:
        _response_threads.popitem(last=False)


def _thread_for_response(response_id: str) -> "tuple[str, str] | None":
    return _response_threads.get(response_id)


def _thread_lock(account_id: str, thread_id: str) -> asyncio.Lock:
    key = (account_id, thread_id)
    lk = _thread_locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _thread_locks[key] = lk
        _thread_locks.move_to_end(key)
        while len(_thread_locks) > _MAX_THREAD_LOCKS:
            _thread_locks.popitem(last=False)
    return lk


async def _resume_thread(worker, account_id: str, thread_id: str) -> None:
    """Navigate `worker` (already pinned to `account_id`) to `thread_id`.

    Asserts the worker really belongs to the owning account — a belt-and-braces
    check on top of the pinned acquire, since resuming on the wrong account would
    silently answer with no context.
    """
    if worker.account_id != account_id:
        # Should be impossible (pinned acquire), but never proceed if it happens.
        raise HTTPException(
            status_code=500,
            detail="internal routing error: worker/account mismatch on resume",
        )
    if worker.extract_thread_id() != thread_id:
        try:
            await worker.navigate_to_thread(thread_id)
        except Exception as e:
            log.error(f"Could not resume thread {thread_id}: {e}")
            raise HTTPException(
                status_code=500, detail=f"Could not resume conversation: {e}"
            )


def _resolve_chain(previous_response_id: str) -> "tuple[str, str]":
    """(account_id, thread_id) for a previous_response_id, or 400 if forgotten."""
    entry = _thread_for_response(previous_response_id)
    if not entry:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown previous_response_id {previous_response_id!r}. This "
                f"gateway tracks the last {_MAX_TRACKED_RESPONSES} responses in "
                "memory and forgets them on restart; start a new conversation."
            ),
        )
    return entry


def _resolve_provider(model: str | None, path_provider: Optional[str] = None) -> str:
    """Which provider should serve this request.

    Precedence: an explicit /v1/<provider>/... path prefix wins; otherwise the
    model id is mapped (chatgpt-browser -> chatgpt, gemini-browser /
    nano-banana-pro -> gemini, plus friendly aliases). Falls back to the default
    provider so old callers keep working.
    """
    if path_provider:
        try:
            return get_provider(path_provider).id
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    mapped = model_id_to_provider(model or "")
    return mapped or Config.DEFAULT_PROVIDER


def _get_model_id(provider: str | None = None) -> str:
    return get_provider(provider or Config.DEFAULT_PROVIDER).chat_model_id


def _resolve_image_model(provider: str, requested: str | None) -> str:
    """Concrete image-model id for this request.

    If the caller named one of the provider's image models (e.g. nano-banana vs
    nano-banana-pro) use it verbatim; otherwise fall back to the provider's
    default image model. This is what lets one endpoint serve both the regular
    and pro variants purely by the `model` field.
    """
    spec = get_provider(provider)
    options = spec.image_models or ((spec.image_model_id,) if spec.image_model_id else ())
    req = (requested or "").strip().lower()
    if req in options:
        return req
    # allow substring aliases like "nanobanana pro" / "nano_banana_pro"
    norm = req.replace("_", "-").replace(" ", "-")
    for opt in options:
        if opt == norm or (opt in norm):
            return opt
    return spec.image_model_id or (options[0] if options else "")


def _resolve_chat_model(provider: str, requested: str | None) -> str:
    """Which of the provider's chat models this request wants, "" for its default.

    The Gemini and Grok web UIs let you pick a model per message (gemini-fast /
    gemini-thinking / grok-expert / grok-deepsearch / ...), so the `model` field
    selects one and the client clicks it in the UI before sending. "" means
    "leave the picker alone" — returned for single-model providers and for any
    id we don't recognise, so an unknown model never silently switches an
    account onto a different (possibly metered) tier.
    """
    return resolve_chat_model(provider, requested or "")


def set_openai_pool(pool) -> None:
    """Called by server.py to inject the worker pool."""
    global _pool
    _pool = pool


def set_usage(usage) -> None:
    """Called by server.py to inject the usage store (for limit accounting)."""
    global _usage
    _usage = usage


def _get_pool():
    if _pool is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return _pool


def _can_retry_unpinned_response(*args, **kwargs) -> bool:
    """Only first-turn Responses calls can safely move to another account."""
    request = kwargs.get("request", args[0] if args else None)
    return not bool(getattr(request, "previous_response_id", None))


@asynccontextmanager
async def _acquire_for_response(pinned_account, chain, provider=None, model=None):
    """Acquire a worker for /v1/responses, pinned to the chain's account.

    When chained, also holds the per-(account,thread) lock across the whole
    request so two continuations of the SAME conversation can never drive that
    one /c/<id> from two tabs at once. Non-chained requests use the normal fair
    acquire and take no thread lock.
    """
    from src.providers.temporary_chat import fresh_chat_required
    if chain and fresh_chat_required():
        raise HTTPException(409, detail="New-chat-per-request mode cannot resume saved threads. Send the context in messages instead.")
    tlock = _thread_lock(*chain) if chain else None
    if tlock is not None:
        await tlock.acquire()
    try:
        async with _get_pool().acquire(
            pinned_account,
            provider,
            model=model,
        ) as worker:
            yield worker
    finally:
        if tlock is not None:
            tlock.release()


async def _observe(worker, result) -> None:
    """Record usage for the account that served this request, and — if the reply
    was a limit banner — take that account offline IMMEDIATELY so no further
    request routes to it (the monitor reactivates it when the cooldown expires).
    """
    aid = worker.account_id
    limit_hit = bool(getattr(result, "limit_hit", False))
    # A limit is a cooldown, not a broken account. Let _Acquired preserve the
    # cooldown state if the endpoint subsequently returns a retryable response.
    if limit_hit:
        worker.limit_hit = True
    if _usage is None:
        return
    _usage.note_request(aid)
    if limit_hit:
        reset = getattr(result, "limit_reset_seconds", 0.0) or 0.0
        _usage.note_limit(aid, reset)
        _usage.note_failure(aid)
        try:
            await _get_pool().set_schedulable(aid, False)
        except Exception:
            pass
        log.warning(f"[{aid}] limit detected — account taken offline "
                    f"(reset in ~{int(reset)}s)")
    else:
        _usage.note_success(aid)


# ── Helpers ─────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token)."""
    return max(1, len(text) // 4)


def _extract_content_text(content) -> str:
    """Extract text from message content (handles both string and list format)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts) if parts else ""
    return str(content)


def _image_url_from_part(part: dict) -> str:
    """Pull the image URL/data-URL out of one content part, across formats.

    Handles the three shapes OpenAI clients send:
      chat vision:      {"type":"image_url",   "image_url":{"url":"..."}}
      responses input:  {"type":"input_image", "image_url":"..."}          (string)
      responses (nested):{"type":"input_image","image_url":{"url":"..."}}
    Returns "" if the part carries no image.
    """
    if not isinstance(part, dict):
        return ""
    if part.get("type") not in ("image_url", "input_image"):
        return ""
    image_url = part.get("image_url", "")
    if isinstance(image_url, dict):
        return image_url.get("url", "") or ""
    return str(image_url) if image_url else ""


def _extract_image_urls(content) -> list[str]:
    """Extract image URLs from message content (OpenAI vision / responses format)."""
    if not isinstance(content, list):
        return []
    return [u for u in (_image_url_from_part(item) for item in content) if u]


async def _download_reference_images(
    specs: list[str], *, endpoint: str, limit: int = 16
) -> list[str]:
    """Decode reference-image specs (data URLs / http URLs) to local file paths.

    Each spec goes through _download_file, which enforces the SSRF guard on
    http(s) URLs and decodes data URLs. Bad entries are skipped with a warning
    rather than failing the whole request. Caller owns cleanup of the paths.
    """
    if not specs:
        return []
    if len(specs) > limit:
        log.warning(f"{endpoint}: {len(specs)} reference images, using first {limit}")
        specs = specs[:limit]
    paths: list[str] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, str) or not spec.strip():
            continue
        local = await _download_file(spec.strip())
        if local:
            paths.append(local)
        else:
            log.warning(f"{endpoint}: reference image #{i + 1} could not be fetched/decoded")
    if paths:
        log.info(f"{endpoint}: attached {len(paths)} reference image(s) to the prompt")
    return paths


def _cleanup_paths(paths: list[str]) -> None:
    """Best-effort delete of temp files (the janitor is the backstop)."""
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


def _extract_file_attachments(content) -> list[dict]:
    """
    Extract file attachments from message content.

    Supported content part format:
      {"type": "file", "file": {"filename": "test.pdf", "data": "base64...", "mime_type": "application/pdf"}}

    Also supports a shorthand data-URL style:
      {"type": "file", "file": {"filename": "test.pdf", "url": "data:application/pdf;base64,..."}}

    Returns list of dicts: [{"filename": str, "data_b64": str, "mime_type": str}, ...]
    """
    if not isinstance(content, list):
        return []
    files = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "file":
            continue
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            continue
        filename = file_info.get("filename", "attachment")
        # Two ways to supply file data:
        # 1. data + mime_type  2. url (data-URL)
        data_b64 = file_info.get("data")
        mime_type = file_info.get("mime_type", "application/octet-stream")
        url = file_info.get("url", "")
        if not data_b64 and url.startswith("data:"):
            # Parse data URL
            try:
                header, data_b64 = url.split(",", 1)
                # header = "data:application/pdf;base64"
                if ":" in header and ";" in header:
                    mime_type = header.split(":")[1].split(";")[0]
            except ValueError:
                continue
        if data_b64:
            files.append({"filename": filename, "data_b64": data_b64, "mime_type": mime_type})
    return files


# Remote fetches are bounded so a hostile/huge URL cannot hang or fill the disk.
_FETCH_TIMEOUT = 30
_FETCH_MAX_BYTES = 25 * 1024 * 1024  # 25 MB


def _assert_fetchable_url(url: str) -> None:
    """Raise ValueError unless `url` is safe for the server to fetch.

    The URL is attacker-controlled (it arrives in the request body as an
    OpenAI-style image_url), and we fetch it from inside the container — which
    sits on a Docker network and, in the cloud, next to a metadata endpoint.
    Without this, the API is an SSRF pivot: it would happily GET
    http://169.254.169.254/latest/meta-data/ or http://localhost:8000/ and
    hand the bytes to ChatGPT.

    Set ALLOW_LOCAL_FETCH=true to permit private/loopback targets (useful when
    images legitimately live on your LAN).
    """
    import ipaddress
    import socket as _socket
    from urllib.parse import urlparse as _urlparse

    parsed = _urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parsed.scheme!r} not allowed (http/https only)")
    host = parsed.hostname
    if not host:
        raise ValueError("no host in URL")

    if os.getenv("ALLOW_LOCAL_FETCH", "false").lower() == "true":
        return

    # Resolve and check EVERY address the name maps to — a hostname can
    # legitimately resolve to a private address (DNS rebinding).
    try:
        infos = _socket.getaddrinfo(host, None)
    except Exception as e:
        raise ValueError(f"cannot resolve host {host!r}: {e}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local      # 169.254.0.0/16 — cloud metadata lives here
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"host {host!r} resolves to non-public address {ip} "
                "(set ALLOW_LOCAL_FETCH=true to permit)"
            )


def _fetch_to_file(url: str, filepath: str) -> None:
    """Blocking bounded download. Run via asyncio.to_thread, never inline."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "miri-api"})
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > _FETCH_MAX_BYTES:
            raise ValueError(f"file too large: {declared} bytes > {_FETCH_MAX_BYTES}")
        # Read with a cap — Content-Length can lie or be absent.
        data = resp.read(_FETCH_MAX_BYTES + 1)
    if len(data) > _FETCH_MAX_BYTES:
        raise ValueError(f"file exceeds {_FETCH_MAX_BYTES} bytes")
    with open(filepath, "wb") as f:
        f.write(data)


async def _download_file(url_or_data: str | dict, download_dir: str = "/tmp/miri_files") -> str | None:
    """
    Download / decode a file (image, PDF, etc.) from URL, base64 data URL,
    or a file attachment dict. Returns the local file path.
    """
    import base64
    import hashlib
    import os

    os.makedirs(download_dir, exist_ok=True)

    # ── Dict form (from _extract_file_attachments) ──
    if isinstance(url_or_data, dict):
        try:
            filename = url_or_data.get("filename", "file")
            data_b64 = url_or_data["data_b64"]
            # Sanitize filename
            safe_name = _UNSAFE_NAME_RE.sub("_", filename)
            hash_suffix = hashlib.md5(data_b64[:60].encode()).hexdigest()[:8]
            filepath = os.path.join(download_dir, f"{hash_suffix}_{safe_name}")
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(data_b64))
            log.info(f"Decoded file attachment: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode file attachment: {e}")
            return None

    # ── String forms ──
    url = str(url_or_data)

    if url.startswith("data:"):
        # Base64 data URL: data:image/png;base64,iVBOR... or data:application/pdf;base64,...
        try:
            header, b64data = url.split(",", 1)
            # Detect extension from MIME type
            ext = "bin"
            mime = ""
            if ":" in header and ";" in header:
                mime = header.split(":")[1].split(";")[0]
            ext_map = {
                "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                "image/gif": "gif", "application/pdf": "pdf",
                "text/plain": "txt", "text/csv": "csv",
                "application/json": "json",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            }
            ext = ext_map.get(mime, mime.split("/")[-1] if "/" in mime else "bin")
            filename = f"file_{hashlib.md5(b64data[:100].encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(b64data))
            log.info(f"Decoded base64 file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to decode base64 data URL: {e}")
            return None
    elif url.startswith(("http://", "https://")):
        # Remote URL — fetch it.
        #
        # This URL comes straight from the caller's request body, so the fetch
        # is guarded (see _assert_fetchable_url) against being used as an SSRF
        # pivot into the container's network — cloud metadata at 169.254.169.254,
        # this API's own port, or anything else on the private network.
        try:
            _assert_fetchable_url(url)
        except ValueError as e:
            log.warning(f"Refusing to fetch {url[:80]}: {e}")
            return None

        try:
            ext = "bin"
            for e in ["jpg", "jpeg", "webp", "gif", "png", "pdf", "txt", "csv", "docx", "xlsx"]:
                if e in url.lower():
                    ext = e
                    break
            filename = f"file_{hashlib.md5(url.encode()).hexdigest()[:12]}.{ext}"
            filepath = os.path.join(download_dir, filename)
            # urlretrieve is blocking with no timeout. Called bare inside this
            # async handler it froze the whole event loop (including /healthz)
            # for the duration of the download, while holding the browser lock.
            await asyncio.to_thread(_fetch_to_file, url, filepath)
            log.info(f"Downloaded file: {filepath}")
            return filepath
        except Exception as e:
            log.error(f"Failed to download file from {url}: {e}")
            return None
    elif os.path.isfile(url):
        # Local file path
        return url
    else:
        log.warning(f"Unknown file URL format: {url[:80]}")
        return None


def _build_prompt(messages: list[ChatMessage]) -> str:
    """
    Flatten an OpenAI-style message array into a single prompt string
    that we can paste into ChatGPT's input box.

    The browser already maintains conversation context within a thread,
    so for simple single-turn calls we just send the last user message.
    For multi-turn with system prompts or tool results, we build a
    formatted transcript.
    """
    # Simple case: only one user message (and optionally one system message)
    non_system = [m for m in messages if m.role != "system"]
    system_msgs = [m for m in messages if m.role == "system"]

    # If it's just one user message, send it directly
    if len(non_system) == 1 and non_system[0].role == "user":
        prefix = ""
        if system_msgs:
            sys_text = _extract_content_text(system_msgs[0].content)
            if Config.PROVIDER == "claude":
                # Claude rejects "[System instruction: ...]" as prompt injection.
                # Present it as context instead.
                prefix = f"{sys_text}\n\n"
            else:
                prefix = f"[System instruction: {sys_text}]\n\n"
        user_text = _extract_content_text(non_system[0].content)
        return prefix + (user_text or "")

    # Multi-turn: build a transcript
    parts: list[str] = []
    for msg in messages:
        role = msg.role.capitalize()
        if msg.role == "system":
            if Config.PROVIDER == "claude":
                # For Claude, present system messages as context without the label
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(text)
            else:
                text = _extract_content_text(msg.content)
                if text:
                    parts.append(f"System: {text}")
        elif msg.role == "tool":
            # Tool result — include both the call context and the result
            tool_content = _extract_content_text(msg.content)
            if Config.PROVIDER == "claude":
                parts.append(
                    f"The tool was executed and returned this result:\n{tool_content}\n\n"
                    f"Now use the result above to answer the user's original question in plain text."
                )
            else:
                parts.append(
                    f"[Tool result for {msg.tool_call_id or 'unknown'}]: {tool_content}\n\n"
                    f"Use the tool result to answer the user. Do NOT call tools again."
                )
        elif msg.role == "assistant" and msg.tool_calls:
            # Assistant requested tool calls — show what was called
            calls_desc = []
            for tc in msg.tool_calls:
                calls_desc.append(
                    f'{tc.function.name}({tc.function.arguments})'
                )
            parts.append(f"Assistant called tools: {', '.join(calls_desc)}")
        elif msg.content:
            text = _extract_content_text(msg.content)
            if text:
                parts.append(f"{role}: {text}")

    return "\n\n".join(parts)


def _build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict | None = None,
) -> str:
    """
    Build a system-level instruction that tells the model about available tools.

    *tool_choice* controls how insistent the instructions are:
      - "auto" / None  — model decides whether to call a tool or answer directly
      - "required"     — model MUST call at least one tool
      - "none"         — caller should not call this function at all
      - {"type":"function","function":{"name":"X"}} — model MUST call that tool
    """
    tool_descriptions = []
    for tool in tools:
        fn = tool.function
        desc = {
            "name": fn.name,
            "description": fn.description,
            "parameters": fn.parameters,
        }
        tool_descriptions.append(json.dumps(desc, indent=2))

    tools_json = "\n---\n".join(tool_descriptions)

    # ── Determine the decision instruction based on tool_choice ──
    forced_tool_name = None
    if isinstance(tool_choice, dict):
        # {"type": "function", "function": {"name": "X"}}
        forced_tool_name = (
            tool_choice.get("function", {}).get("name")
            if isinstance(tool_choice.get("function"), dict)
            else None
        )

    if forced_tool_name:
        decision = (
            f"You MUST call the function `{forced_tool_name}`. "
            f"Do NOT answer the question yourself — output only the JSON tool call."
        )
    elif tool_choice == "required":
        decision = (
            "You MUST call at least one of the available functions. "
            "Do NOT answer the question yourself — always output tool calls."
        )
    else:
        # "auto" or None — model decides
        decision = (
            "If the user's request can be fulfilled or assisted by one or more "
            "of the available functions, call the appropriate tool(s). "
            "If none of the tools are relevant, answer the user normally in plain text."
        )

    # ── Provider-specific prompt framing ──
    if Config.PROVIDER == "claude":
        return f"""You have access to external tools through a structured interface. {decision}

When calling tools, respond with ONLY a JSON code block — no text before or after it:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. Do not add any commentary, explanation, or text outside the code block.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types shown in each function's schema.
4. When you receive tool results in a follow-up message, use them to give the user a natural, helpful answer. Do NOT output another JSON tool call for the same request.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""
    else:
        return f"""You are in tool-calling mode. {decision}

When calling tools, output ONLY a JSON code block — no other text:

```json
{{"tool_calls": [{{"name": "<function_name>", "arguments": {{...}}}}]}}
```

Rules:
1. Output ONLY the JSON code block when calling tools. No explanation, no text before or after.
2. You may call multiple functions in one response by adding them to the array.
3. Use the exact parameter names and types from each function's schema.
4. When a follow-up message contains tool results, summarize them naturally for the user. Do NOT call tools again for the same request.
5. Do not refuse or say tools are unavailable — they are available through this interface.

Available functions:
{tools_json}

Example — single tool:
```json
{{"tool_calls": [{{"name": "get_current_time", "arguments": {{}}}}]}}
```

Example — multiple tools:
```json
{{"tool_calls": [{{"name": "weather_forecast", "arguments": {{"city": "Tokyo", "date": "today"}}}}, {{"name": "calculate_expression", "arguments": {{"expression": "2+2"}}}}]}}
```
"""


def _extract_json_object(text: str, anchor: str = "tool_calls") -> str | None:
    """
    Extract a JSON object containing *anchor* key from *text*.

    Uses two strategies:
      1. Look inside markdown code blocks (```json ... ```)
      2. Find the anchor key and walk outward using brace-depth tracking
         to handle arbitrarily nested JSON (arrays, nested objects, etc.)
    """
    # Strategy 1: code blocks — most reliable when the model obeys the prompt
    for m in _CODE_BLOCK_RE.finditer(text):
        candidate = m.group(1).strip()
        if anchor in candidate:
            try:
                parsed = json.loads(candidate)
                if anchor in parsed:
                    return candidate
            except json.JSONDecodeError:
                continue

    # Strategy 2: locate anchor, walk to balanced braces
    search_key = f'"{anchor}"'
    idx = text.find(search_key)
    if idx == -1:
        return None

    # Walk backward to the nearest '{'
    start = text.rfind("{", 0, idx)
    if start == -1:
        return None

    # Walk forward tracking brace depth, respecting JSON string literals
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2          # skip escaped char
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        return None
        i += 1

    return None


def _parse_tool_calls(
    response_text: str, tools: list[ToolDefinition]
) -> list[ToolCall] | None:
    """
    Try to parse tool calls from the model's response text.

    Uses robust brace-matching extraction (handles nested JSON, arrays, etc.)
    then validates tool names against the provided tool definitions.
    Returns None if no valid tool calls are found.
    """
    json_str = _extract_json_object(response_text, "tool_calls")
    if not json_str:
        return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        log.debug(f"Failed to parse tool call JSON: {json_str[:200]}")
        return None

    if "tool_calls" not in parsed or not isinstance(parsed["tool_calls"], list):
        return None

    # Validate that the called functions are in the provided tools
    valid_names = {t.function.name for t in tools}
    result: list[ToolCall] = []

    for call in parsed["tool_calls"]:
        name = call.get("name", "")
        if name not in valid_names:
            log.warning(f"Model called unknown tool: {name}")
            continue

        arguments = call.get("arguments", {})
        if isinstance(arguments, dict):
            arguments_str = json.dumps(arguments)
        else:
            arguments_str = str(arguments)

        result.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                type="function",
                function=FunctionCallInfo(name=name, arguments=arguments_str),
            )
        )

    return result if result else None


# ── Routes ──────────────────────────────────────────────────────


# Upload limits for /v1/images/edits. 16 matches the OpenAI spec; each file is
# capped so a hostile client cannot fill the container's disk.
_MAX_EDIT_IMAGES = 16
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per image


# ── Image parameter support matrix ──────────────────────────────
#
# The backend is the ChatGPT web UI, not the OpenAI Images API. Most documented
# params have no control surface in a web page. Every param is ACCEPTED (so
# unmodified OpenAI SDK clients work), but here is what actually happens:
#
#   HONORED        we really do it
#   POST-PROCESSED we do it ourselves, after scraping the image
#   PROMPT-HINT    appended to the prompt; the model may ignore it
#   IGNORED        accepted and discarded (a warning is logged)
#
# The response reports what was ACTUALLY produced, never what was requested.
IMAGE_PARAM_SUPPORT = {
    "prompt": "HONORED",
    "n": "PROMPT-HINT",           # we ask for N images; the UI decides
    "size": "PROMPT-HINT",        # cannot set pixels; the model picks
    "quality": "PROMPT-HINT",
    "background": "PROMPT-HINT",  # "transparent" rarely obeyed
    "style": "PROMPT-HINT",
    "output_format": "POST-PROCESSED",       # we convert png -> jpeg/webp
    "output_compression": "POST-PROCESSED",  # encoder quality on jpeg/webp
    "response_format": "HONORED",
    "model": "IGNORED",           # UI has no model picker
    "moderation": "IGNORED",      # UI-side, not controllable
    "stream": "IGNORED",          # no partial frames to scrape
    "partial_images": "IGNORED",
    "input_fidelity": "IGNORED",  # edits: UI re-generates, cannot pin pixels
    "mask": "IGNORED",            # edits: no inpainting surface in the UI
    "user": "IGNORED",
}

_IGNORED_PARAMS = {k for k, v in IMAGE_PARAM_SUPPORT.items() if v == "IGNORED"}
_IGNORED_PARAMS_SORTED = tuple(sorted(_IGNORED_PARAMS))  # precomputed once


def _warn_ignored(request_dict: dict, endpoint: str) -> list[str]:
    """Log any set-but-unhonorable params. Returns their names."""
    ignored = []
    for name in _IGNORED_PARAMS_SORTED:
        val = request_dict.get(name)
        if val in (None, "", False, 0):
            continue
        if name == "model":
            continue  # every SDK sends model; warning on it is pure noise
        ignored.append(name)
    if ignored:
        log.warning(
            f"{endpoint}: ignoring params the ChatGPT web UI cannot honor: "
            f"{', '.join(ignored)}"
        )
    return ignored


def _build_image_prompt(base: str, *, n: int = 1, size: str = "auto",
                        quality: str = "auto", background: str = "auto",
                        style: str | None = None) -> str:
    """Fold the hint-able params into the prompt text.

    These are hints, not settings: the model routinely ignores them. That is
    why the response echoes the REAL output size rather than the requested one.
    """
    parts = [base]
    if n and n > 1:
        parts.append(f"Please generate {n} different images.")
    if size and size != "auto" and size != "1024x1024":
        parts.append(f"Image size: {size}.")
    if quality == "high":
        parts.append("Make it high-definition / highly detailed.")
    elif quality == "low":
        parts.append("A quick, low-detail draft is fine.")
    if background == "transparent":
        parts.append("The image must have a transparent background.")
    if style == "natural":
        parts.append("Use a natural, realistic style.")
    elif style == "vivid":
        parts.append("Use a vivid, dramatic style.")
    return " ".join(parts)


def _convert_image(raw: bytes, output_format: str, compression: int | None) -> tuple[bytes, str]:
    """Convert scraped PNG bytes to the requested format. Returns (bytes, format).

    This is the one image param we can genuinely honor, because we do it
    ourselves after scraping. Falls back to the original bytes if Pillow can't
    handle it — never fails the request over a format preference.
    """
    fmt = (output_format or "png").lower()
    if fmt == "png":
        return raw, "png"
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        out = io.BytesIO()
        if fmt in ("jpeg", "jpg"):
            # JPEG has no alpha channel; flatten onto white first.
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            img.save(out, format="JPEG", quality=compression if compression is not None else 85)
            return out.getvalue(), "jpeg"
        if fmt == "webp":
            img.save(out, format="WEBP", quality=compression if compression is not None else 85)
            return out.getvalue(), "webp"
        log.warning(f"Unknown output_format {fmt!r} — returning png")
        return raw, "png"
    except Exception as e:
        log.warning(f"Image conversion to {fmt} failed ({e}) — returning png")
        return raw, "png"


def _is_readable_image(raw: bytes) -> bool:
    """True if Pillow can parse `raw` as an image. Blocking — call via to_thread."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()
        return True
    except Exception:
        return False


def _image_size_str(raw: bytes) -> str | None:
    """Actual WxH of the produced image, for honest reporting."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            return f"{img.size[0]}x{img.size[1]}"
    except Exception:
        return None


def _build_image_usage(prompt: str, n_input_images: int, output_bytes: int) -> ImageUsage:
    """ESTIMATED usage. The web UI reports nothing, so this is derived."""
    text_tokens = _estimate_tokens(prompt)
    # OpenAI bills ~85 tokens per low-detail input image; use it as a stand-in.
    image_tokens = 85 * n_input_images
    output_tokens = max(1, output_bytes // 750)  # rough, scales with image size
    return ImageUsage(
        input_tokens=text_tokens + image_tokens,
        output_tokens=output_tokens,
        total_tokens=text_tokens + image_tokens + output_tokens,
        input_tokens_details=ImageUsageDetails(
            image_tokens=image_tokens, text_tokens=text_tokens
        ),
    )


@openai_router.get("/v1/models", response_model=ModelListResponse)
async def list_models() -> ModelListResponse:
    """List available models — returns our single browser-backed model."""
    # Every provider we can serve, so an OpenAI SDK client can discover both
    # chatgpt and gemini models from one call.
    owners = {"chatgpt": "openai", "gemini": "google", "grok": "xai",
              "claude": "anthropic", "seedream": "bytedance",
              "deepseek": "deepseek", "qwen": "alibaba"}
    data = []
    for pid in ENABLED_PROVIDER_IDS:
        spec = get_provider(pid)
        # Chat models. Gemini and Grok expose every model their web UI lets
        # you pick (gemini-thinking, grok-expert, ...); the others have one.
        # Media-only providers like Seedream have none.
        if spec.supports_chat:
            chats = spec.chat_models or ((spec.chat_model_id,) if spec.chat_model_id else ())
            for mid in chats:
                data.append(ModelObject(id=mid, owned_by=owners.get(pid, pid)))
        # Every selectable image model (e.g. nano-banana AND nano-banana-pro).
        imgs = spec.image_models or ((spec.image_model_id,) if spec.image_model_id else ())
        for mid in imgs:
            if spec.supports_images and mid:
                data.append(ModelObject(id=mid, owned_by=owners.get(pid, pid)))
        # Every selectable video model (Seedream: seedance-2.0, video-3.0, ...).
        if spec.supports_video:
            for mid in (spec.video_models or ((spec.video_model_id,) if spec.video_model_id else ())):
                if mid:
                    data.append(ModelObject(id=mid, owned_by=owners.get(pid, pid)))
    return ModelListResponse(data=data)


@openai_router.get("/v1/chat/models")
@openai_router.get("/v1/{path_provider}/chat/models")
async def list_chat_models(path_provider: Optional[str] = None) -> dict:
    """The CHAT models each provider serves, with what each one is for.

    /v1/models answers "which ids exist" in OpenAI's shape; this answers "what
    does gemini-thinking actually do, and is it one of the slow ones" — the
    part an OpenAI ModelObject has nowhere to put. `long: true` marks models
    that work for minutes per answer (Gemini Deep Research / Deep Think, Grok
    DeepSearch / Heavy) and are held to LONG_MODE_TIMEOUT instead of
    RESPONSE_TIMEOUT.
    """
    if path_provider is not None:
        try:
            pids = [get_provider(path_provider).id]
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    else:
        pids = [pid for pid in ENABLED_PROVIDER_IDS if get_provider(pid).supports_chat]

    return {
        "object": "list",
        "data": [
            {
                "provider": pid,
                "label": get_provider(pid).label,
                "default": get_provider(pid).chat_model_id,
                "models": chat_model_catalog(pid),
            }
            for pid in pids
        ],
    }


# NOTE: declared AFTER /v1/chat/models — this pattern would otherwise capture
# "chat" as a provider name and 404 that route.
@openai_router.get("/v1/{path_provider}/models", response_model=ModelListResponse)
async def list_provider_models(path_provider: str) -> ModelListResponse:
    """Just ONE provider's models — the /v1/<P>/... counterpart of /v1/models.

    Same shape as /v1/models, filtered to `path_provider`. Useful now that a
    provider can serve a dozen chat models: a client pointed at
    /v1/gemini/... can discover exactly what that path accepts.
    """
    try:
        spec = get_provider(path_provider)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    owners = {"chatgpt": "openai", "gemini": "google", "grok": "xai",
              "claude": "anthropic", "seedream": "bytedance",
              "deepseek": "deepseek", "qwen": "alibaba"}
    owner = owners.get(spec.id, spec.id)
    data = []
    if spec.supports_chat:
        for mid in (spec.chat_models or ((spec.chat_model_id,) if spec.chat_model_id else ())):
            data.append(ModelObject(id=mid, owned_by=owner))
    if spec.supports_images:
        for mid in (spec.image_models or ((spec.image_model_id,) if spec.image_model_id else ())):
            if mid:
                data.append(ModelObject(id=mid, owned_by=owner))
    if spec.supports_video:
        for mid in (spec.video_models or ((spec.video_model_id,) if spec.video_model_id else ())):
            if mid:
                data.append(ModelObject(id=mid, owned_by=owner))
    return ModelListResponse(data=data)


async def _run_image_request(
    worker,
    *,
    prompt: str,
    endpoint: str,
    response_format: str,
    output_format: str,
    output_compression: int | None,
    background: str,
    quality: str,
    image_paths: list[str] | None = None,
    image_model: str = "",
    http_request: Request | None = None,
) -> ImagesResponse:
    """Shared pipeline for /v1/images/generations and /v1/images/edits.

    Both endpoints are the same operation to a browser: put a prompt (and
    optionally some images) into the composer, wait, scrape the produced image.
    generations = text -> image; edits = text + image(s) -> image.

    Caller must already hold `worker` (checked out from the pool).
    """
    client = worker.client
    start_time = time.time()

    await worker.ensure_fresh_chat()

    try:
        result = await client.send_message(
            prompt, image_paths=image_paths or None, expect_image=True,
            image_model=image_model,
        )
    except Exception as e:
        log.error(f"Provider error during {endpoint}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Provider error: {str(e)}")

    elapsed_ms = int((time.time() - start_time) * 1000)

    if not result.images:
        # The model answered with text instead of an image — declined, asked a
        # clarifying question, hit a content filter, or hit the usage LIMIT.
        # Observe first: if it was a limit, that takes the account offline and
        # returns 503 (retryable) rather than 422.
        await _observe(worker, result)
        log.warning(
            f"No images detected in response ({elapsed_ms}ms). "
            f"ChatGPT replied: {result.message[:200]}"
        )
        if getattr(result, "limit_hit", False):
            raise HTTPException(
                status_code=503,
                detail=f"Account hit its usage limit: {result.message[:300]}",
                headers={"Retry-After": "60"},
            )
        raise HTTPException(
            status_code=422,
            detail=f"{get_provider(worker.provider).label} did not generate an image. Model response: {result.message[:500]}",
        )

    image_data_list: list[ImageData] = []
    actual_size: str | None = None
    actual_format = "png"
    total_bytes = 0

    for img_info in result.images:
        revised_prompt = img_info.prompt_title or img_info.alt or prompt
        if not img_info.local_path:
            log.warning(f"Image has no local_path: {img_info.url[:80]}")
            continue
        try:
            with open(img_info.local_path, "rb") as f:
                raw = f.read()
        except Exception as e:
            log.error(f"Failed to read image file {img_info.local_path}: {e}")
            continue

        # Report the real dimensions, not the requested ones — the UI picks the
        # size and routinely ignores the hint.
        #
        # Pillow decode/encode is blocking CPU work on multi-MB images. Run it
        # off the event loop: inline, it freezes every other request and
        # /healthz for the duration, which is what marks the container
        # unhealthy. Same reason the downloads use to_thread.
        actual_size = actual_size or await asyncio.to_thread(_image_size_str, raw)

        converted, actual_format = await asyncio.to_thread(
            _convert_image, raw, output_format, output_compression
        )
        total_bytes += len(converted)

        if response_format == "b64_json":
            image_data_list.append(
                ImageData(
                    b64_json=base64.b64encode(converted).decode("utf-8"),
                    revised_prompt=revised_prompt,
                )
            )
        else:
            # url form: hand back a URL that actually resolves. If we converted
            # the bytes, write the converted file so the link matches what we
            # reported. A file we cannot serve degrades to b64 rather than
            # returning a path the caller would fetch and get a 404 from.
            path = img_info.local_path
            if actual_format != "png":
                path = str(Path(path).with_suffix("." + actual_format))
                try:
                    Path(path).write_bytes(converted)
                except Exception as e:
                    log.warning(f"Could not write converted image: {e}")
                    path = img_info.local_path
            url = media_url(path, kind="images", request=http_request)
            if url:
                image_data_list.append(ImageData(
                    url=url, local_path=path, revised_prompt=revised_prompt))
            else:
                log.warning("Image %s cannot be served over HTTP — returning it inline instead",
                            path)
                image_data_list.append(ImageData(
                    b64_json=base64.b64encode(converted).decode("utf-8"),
                    local_path=path, revised_prompt=revised_prompt))

    if not image_data_list:
        raise HTTPException(
            status_code=500, detail="Images were detected but could not be processed."
        )

    log.info(
        f"{endpoint} complete [{worker.account_id}]: {len(image_data_list)} image(s), "
        f"{elapsed_ms}ms, size={actual_size}, format={actual_format}"
    )
    await _observe(worker, result)
    worker.increment_thread_count()

    return ImagesResponse(
        data=image_data_list,
        # Echo what actually happened. background/quality are hints the UI does
        # not report back, so we state the neutral truth rather than parroting
        # the request and implying we honored it.
        background="opaque" if background != "transparent" else "auto",
        output_format=actual_format,
        quality="auto",
        size=actual_size,
        usage=_build_image_usage(prompt, len(image_paths or []), total_bytes),
    )


@openai_router.post("/v1/images/generations", response_model=ImagesResponse)
@openai_router.post("/v1/{path_provider}/images/generations", response_model=ImagesResponse)
@retry_across_accounts()
async def create_image(request: ImageGenerationRequest,
                       http_request: Request,
                       path_provider: Optional[str] = None) -> ImagesResponse:
    """
    OpenAI-compatible image generation — text to image.

    Backed by the ChatGPT web UI, so most OpenAI params are accepted but cannot
    be honored; see IMAGE_PARAM_SUPPORT. The response reports what was actually
    produced.
    """
    if not request.prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    provider = _resolve_provider(request.model, path_provider)
    spec = get_provider(provider)
    if not spec.supports_images:
        raise HTTPException(
            status_code=501,
            detail=f"{spec.label} does not support image generation.",
        )

    _warn_ignored(request.model_dump(), "POST /v1/images/generations")
    if request.stream:
        # The real API streams partial frames. We scrape a finished <img> out of
        # the DOM — there are no intermediate frames to emit. Failing loudly
        # beats silently returning a non-streamed body to a streaming client.
        raise HTTPException(
            status_code=400,
            detail=(
                "stream=true is not supported: this gateway scrapes the finished "
                "image from the ChatGPT web UI and has no partial frames to send."
            ),
        )

    # Reference images: uploaded to ChatGPT with the prompt as visual context.
    ref_specs = request.image if isinstance(request.image, list) else (
        [request.image] if request.image else []
    )
    ref_paths = await _download_reference_images(
        ref_specs, endpoint="/v1/images/generations"
    )

    log.info(
        f"POST /v1/images/generations — prompt='{request.prompt[:80]}', "
        f"n={request.n}, size={request.size}, output_format={request.output_format}, "
        f"reference_images={len(ref_paths)}"
    )

    base = (
        # When references are attached, tell the model to use them.
        f"Using the attached image(s) as reference, generate an image: {request.prompt}"
        if ref_paths
        else f"Generate an image: {request.prompt}"
    )
    full_prompt = _build_image_prompt(
        base,
        n=request.n or 1,
        size=request.size or "auto",
        quality=request.quality or "auto",
        background=request.background or "auto",
        style=request.style,
    )

    try:
        async with _get_pool().acquire(
            provider=provider,
            model=_resolve_image_model(provider, request.model),
        ) as worker:
            return await _run_image_request(
                worker,
                prompt=full_prompt,
                endpoint="/v1/images/generations",
                response_format=request.response_format or "b64_json",
                output_format=request.output_format or "png",
                output_compression=request.output_compression,
                background=request.background or "auto",
                quality=request.quality or "auto",
                image_paths=ref_paths or None,
                image_model=_resolve_image_model(provider, request.model),
                http_request=http_request,
            )
    finally:
        _cleanup_paths(ref_paths)


@openai_router.post("/v1/images/edits", response_model=ImagesResponse)
@openai_router.post("/v1/{path_provider}/images/edits", response_model=ImagesResponse)
@retry_across_accounts()
async def create_image_edit(
    http_request: Request,
    path_provider: Optional[str] = None,
    prompt: str = Form(..., max_length=32000),
    image: list[UploadFile] = File(...),
    mask: Optional[UploadFile] = File(None),
    model: Optional[str] = Form("chatgpt-image-latest"),
    n: Optional[int] = Form(1),
    size: Optional[str] = Form("auto"),
    quality: Optional[str] = Form("auto"),
    background: Optional[str] = Form("auto"),
    output_format: Optional[str] = Form("png"),
    output_compression: Optional[int] = Form(None),
    input_fidelity: Optional[str] = Form(None),
    moderation: Optional[str] = Form("auto"),
    response_format: Optional[str] = Form("b64_json"),
    stream: Optional[bool] = Form(False),
    partial_images: Optional[int] = Form(0),
    user: Optional[str] = Form(None),
) -> ImagesResponse:
    """
    OpenAI-compatible image edit — image(s) + text to image.

    multipart/form-data. Repeat the `image` field for multiple inputs (max 16).

    IMPORTANT — this is not inpainting. The ChatGPT web UI has no mask surface,
    so it RE-GENERATES the picture guided by your input rather than painting
    inside a masked region. Consequences, measured:
      - output dimensions do NOT match the input
      - pixels outside the edited area are NOT preserved byte-for-byte
      - `mask` and `input_fidelity` are accepted and ignored
    Use it as "image + instruction -> new image", which is what it reliably does.
    """
    provider = _resolve_provider(model, path_provider)
    spec = get_provider(provider)
    if not spec.supports_images:
        raise HTTPException(
            status_code=501,
            detail=f"{spec.label} does not support image editing.",
        )
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty")
    if not image:
        raise HTTPException(status_code=400, detail="at least one `image` file is required")
    if len(image) > _MAX_EDIT_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"at most {_MAX_EDIT_IMAGES} images allowed, got {len(image)}",
        )
    if stream:
        raise HTTPException(
            status_code=400,
            detail=(
                "stream=true is not supported: this gateway scrapes the finished "
                "image from the ChatGPT web UI and has no partial frames to send."
            ),
        )

    _warn_ignored(
        {
            "model": model, "moderation": moderation, "user": user,
            "input_fidelity": input_fidelity, "partial_images": partial_images,
            "mask": mask.filename if mask else None,
        },
        "POST /v1/images/edits",
    )

    # Persist uploads so the browser can attach them from disk. This directory
    # is swept by the janitor (src/janitor.py swept_directories()).
    upload_dir = Path("/tmp/miri_files")
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        for idx, up in enumerate(image):
            raw = await up.read()
            if not raw:
                raise HTTPException(status_code=400, detail=f"image #{idx + 1} is empty")
            if len(raw) > _MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"image #{idx + 1} exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
                )
            # Verify it really is an image before handing it to the browser.
            # Off the event loop: this decodes header+data for up to 16 files of
            # up to 25MB each, which inline would stall every other request.
            if not await asyncio.to_thread(_is_readable_image, raw):
                raise HTTPException(
                    status_code=400,
                    detail=f"image #{idx + 1} ({up.filename!r}) is not a readable image",
                )
            suffix = Path(up.filename or "").suffix.lower() or ".png"
            if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                suffix = ".png"
            dest = upload_dir / f"edit_{uuid.uuid4().hex[:12]}{suffix}"
            dest.write_bytes(raw)
            saved.append(str(dest))

        log.info(
            f"POST /v1/images/edits — prompt='{prompt[:60]}', {len(saved)} image(s), "
            f"mask={'yes (ignored)' if mask else 'no'}, output_format={output_format}"
        )

        full_prompt = _build_image_prompt(
            f"Edit the attached image as follows: {prompt}. Return the edited image.",
            n=n or 1,
            size=size or "auto",
            quality=quality or "auto",
            background=background or "auto",
        )

        async with _get_pool().acquire(
            provider=provider,
            model=_resolve_image_model(provider, model),
        ) as worker:
            return await _run_image_request(
                worker,
                prompt=full_prompt,
                endpoint="/v1/images/edits",
                response_format=response_format or "b64_json",
                output_format=output_format or "png",
                output_compression=output_compression,
                background=background or "auto",
                quality=quality or "auto",
                image_paths=saved,
                image_model=_resolve_image_model(provider, model),
                http_request=http_request,
            )
    finally:
        # The browser has already uploaded the bytes by now; the janitor would
        # eventually reap these anyway, but do not leave them lying around.
        for p in saved:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


# ── Video generation (Seedream / Dreamina, Qwen / Wan) ──────────────────────
#
# The Dreamina web UI is the authority on what an account/plan can do. Every
# documented control is ACCEPTED — model, mode (text/image-to-video, first+last
# frame, multiframe, omni reference), duration, aspect, resolution — and the ones
# the UI exposes are driven; the response reports what was actually produced.
_VIDEO_MAX_REFS = 12  # Omni Reference tops out around 12 files


def _resolve_video_provider(model: str | None, path_provider: Optional[str]) -> str:
    """Which provider serves a video request. Path prefix wins; else map the
    model id; else the first video-capable provider."""
    if path_provider:
        try:
            return get_provider(path_provider).id
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    mapped = model_id_to_provider(model or "")
    if mapped and get_provider(mapped).supports_video:
        return mapped
    # No usable model and no path: Seedream stays the default. It was the only
    # video provider before Qwen gained video, and Qwen sits EARLIER in
    # ENABLED_PROVIDER_IDS — "first video-capable provider" alone would silently
    # reroute every existing request that names no video model to Qwen.
    preferred = ("seedream",) + tuple(p for p in ENABLED_PROVIDER_IDS if p != "seedream")
    for pid in preferred:
        if pid in ENABLED_PROVIDER_IDS and get_provider(pid).supports_video:
            return pid
    raise HTTPException(status_code=501, detail="No video-capable provider is configured.")


def _resolve_video_model(provider: str, requested: str | None) -> str:
    """Concrete video-model id for this request (see seedream/models.py)."""
    spec = get_provider(provider)
    options = spec.video_models or ((spec.video_model_id,) if spec.video_model_id else ())
    if not options:
        return ""
    if provider == "seedream":
        req = (requested or "").strip() or Config.SEEDREAM_DEFAULT_MODEL or ""
        return seedream_models.resolve_model_id(req)
    if provider == "qwen":
        return qwen_models.resolve_video_model_id(requested or "")
    req = (requested or "").strip().lower()
    if req in options:
        return req
    return spec.video_model_id or options[0]


def _resolve_video_mode(request: VideoGenerationRequest) -> "tuple[str, list[str], list[str]]":
    """Decide the generation mode and gather its image specs, in order.

    Returns (mode, image_specs, extra_prompt_lines). The mode is taken from
    `mode` when given, else inferred from which image fields are set."""
    def as_list(x):
        if x is None:
            return []
        return list(x) if isinstance(x, list) else [x]

    first, last = request.first_frame, request.last_frame
    frames = request.frames or []
    refs = request.reference_images or []
    img = as_list(request.image)

    forced = (request.mode or "").strip().lower() or None
    if forced and forced not in seedream_models.MODES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown mode {forced!r}; one of {', '.join(seedream_models.MODES)}",
        )

    mode = forced
    if mode is None:
        if frames:
            mode = seedream_models.MODE_MULTIFRAME
        elif first or last:
            mode = seedream_models.MODE_FIRST_LAST
        elif refs:
            mode = seedream_models.MODE_OMNI
        elif img:
            mode = seedream_models.MODE_I2V
        else:
            mode = seedream_models.MODE_TEXT

    extra: list[str] = []
    if mode == seedream_models.MODE_TEXT:
        images: list = []
    elif mode == seedream_models.MODE_I2V:
        images = (img[:1]) or ([first] if first else [])
    elif mode == seedream_models.MODE_FIRST_LAST:
        images = [x for x in (first, last) if x] or img[:2]
    elif mode == seedream_models.MODE_MULTIFRAME:
        images = [f.image for f in frames if getattr(f, "image", None)] or img
        # Per-transition prompts can't be typed into the UI's between-frame slots
        # blind, so fold them into the prompt as guidance (best-effort, honest).
        tp = [
            f"frame {i + 1}->{i + 2}: {f.transition_prompt}"
            for i, f in enumerate(frames)
            if getattr(f, "transition_prompt", None)
        ]
        if tp:
            extra.append("Shot transitions: " + "; ".join(tp))
    elif mode == seedream_models.MODE_OMNI:
        images = list(refs) or img
    else:
        images = img

    return mode, [s for s in images if isinstance(s, str) and s.strip()], extra


@openai_router.post("/v1/videos/generations", response_model=VideosResponse)
@openai_router.post("/v1/{path_provider}/videos/generations", response_model=VideosResponse)
@retry_across_accounts()
async def create_video(request: VideoGenerationRequest,
                       http_request: Request,
                       path_provider: Optional[str] = None) -> VideosResponse:
    """Text/-image-to-video generation via a provider's web UI.

    Seedream (Dreamina) serves every mode; Qwen (Wan) serves text_to_video and
    image_to_video. Modes (inferred from fields or forced with `mode`):
      text_to_video · image_to_video · first_last_frame · multiframe · omni_reference
    """
    provider = _resolve_video_provider(request.model, path_provider)
    spec = get_provider(provider)
    if not spec.supports_video:
        raise HTTPException(
            status_code=501,
            detail=f"{spec.label} does not support video generation.",
        )

    mode, image_specs, extra = _resolve_video_mode(request)
    # Reject a mode the provider can't do HERE, before a tab is checked out and
    # reference images are downloaded — not as a vague failure minutes later.
    if spec.video_modes and mode not in spec.video_modes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{spec.label} video supports {', '.join(spec.video_modes)} — not "
                f"'{mode}'. For a start image use `image` (image_to_video); "
                "first/last-frame, keyframe and multi-reference modes need a "
                "provider that offers them (seedream)."
            ),
        )
    lo, hi = seedream_models.MODE_IMAGE_BOUNDS.get(mode, (0, None))
    if len(image_specs) < lo:
        raise HTTPException(
            status_code=400,
            detail=f"mode '{mode}' requires at least {lo} image(s); got {len(image_specs)}.",
        )
    if hi and len(image_specs) > hi:
        log.warning(f"/v1/videos/generations: {len(image_specs)} images for '{mode}', using first {hi}")
        image_specs = image_specs[:hi]
    if mode == seedream_models.MODE_TEXT and not (request.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt cannot be empty for text_to_video")

    model_id = _resolve_video_model(provider, request.model)

    image_paths = await _download_reference_images(
        image_specs, endpoint="/v1/videos/generations", limit=_VIDEO_MAX_REFS
    )
    if len(image_paths) < lo:
        _cleanup_paths(image_paths)
        raise HTTPException(
            status_code=400,
            detail=f"could not fetch the required image input(s) for mode '{mode}'.",
        )

    prompt = request.prompt or ""
    if extra:
        prompt = (prompt + "\n\n" + "\n".join(extra)).strip()

    log.info(
        f"POST /v1/videos/generations — provider={provider}, model={model_id}, "
        f"mode={mode}, images={len(image_paths)}, aspect={request.aspect_ratio}, "
        f"res={request.resolution}, dur={request.duration_s}, prompt='{prompt[:60]}'"
    )

    try:
        async with _get_pool().acquire(provider=provider, model=model_id) as worker:
            client = worker.client
            if not hasattr(client, "generate_video"):
                raise HTTPException(status_code=500, detail="provider cannot generate video")
            start_time = time.time()
            try:
                result = await client.generate_video(
                    prompt=prompt,
                    mode=mode,
                    model_id=model_id,
                    image_paths=image_paths or None,
                    duration_s=request.duration_s,
                    aspect_ratio=request.aspect_ratio or "",
                    resolution=request.resolution or "",
                    audio=request.audio,
                )
            except HTTPException:
                raise
            except Exception as e:
                log.error(f"Provider error during video generation: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Provider error: {str(e)}")
            elapsed_ms = int((time.time() - start_time) * 1000)

            if not result.videos:
                await _observe(worker, result)
                if getattr(result, "limit_hit", False):
                    raise HTTPException(
                        status_code=503,
                        detail=f"Account hit its usage/credit limit: {result.message[:300]}",
                        headers={"Retry-After": "120"},
                    )
                raise HTTPException(
                    status_code=422,
                    detail=f"{spec.label} did not produce a video. {result.message[:400]}",
                )

            data_list: list[VideoData] = []
            total_bytes = 0
            duration = None
            for v in result.videos:
                if not v.local_path:
                    continue
                revised = v.prompt_title or prompt
                if request.response_format == "b64_json":
                    try:
                        raw = Path(v.local_path).read_bytes()
                    except Exception as e:
                        log.error(f"Failed to read video {v.local_path}: {e}")
                        continue
                    total_bytes += len(raw)
                    data_list.append(VideoData(
                        b64_json=base64.b64encode(raw).decode("utf-8"),
                        revised_prompt=revised, mime_type=v.mime_type,
                        duration_s=v.duration_s or None,
                    ))
                else:
                    url = media_url(v.local_path, kind="videos", request=http_request)
                    if not url:
                        # Never hand back a filesystem path as a URL: the caller
                        # fetches it and gets a 404 from its own gateway.
                        log.error("Video %s cannot be served over HTTP — returning it inline",
                                  v.local_path)
                        try:
                            raw = Path(v.local_path).read_bytes()
                        except Exception as e:
                            log.error(f"Failed to read video {v.local_path}: {e}")
                            continue
                        total_bytes += len(raw)
                        data_list.append(VideoData(
                            b64_json=base64.b64encode(raw).decode("utf-8"),
                            local_path=v.local_path, revised_prompt=revised,
                            mime_type=v.mime_type, duration_s=v.duration_s or None,
                        ))
                        duration = duration or (v.duration_s or None)
                        continue
                    data_list.append(VideoData(
                        url=url, local_path=v.local_path, revised_prompt=revised,
                        mime_type=v.mime_type, duration_s=v.duration_s or None,
                    ))
                    try:
                        total_bytes += Path(v.local_path).stat().st_size
                    except Exception:
                        pass
                duration = duration or (v.duration_s or None)

            if not data_list:
                # The account still spent a generation — record it (and run the
                # limit check) before failing, like the no-videos path above.
                await _observe(worker, result)
                raise HTTPException(status_code=500, detail="Video produced but could not be read.")

            await _observe(worker, result)
            worker.increment_thread_count()
            log.info(
                f"/v1/videos/generations complete [{worker.account_id}]: "
                f"{len(data_list)} video(s), {elapsed_ms}ms, model={model_id}, mode={mode}"
            )
            return VideosResponse(
                data=data_list,
                model=model_id,
                mode=mode,
                duration_s=duration,
                usage=_build_image_usage(prompt, len(image_paths), total_bytes),
            )
    finally:
        _cleanup_paths(image_paths)


@openai_router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
@openai_router.post("/v1/{path_provider}/chat/completions", response_model=ChatCompletionResponse)
@retry_across_accounts()
async def create_chat_completion(
    request: ChatCompletionRequest,
    path_provider: Optional[str] = None,
) -> ChatCompletionResponse:
    """
    OpenAI-compatible chat completions endpoint.

    Converts the message array into a single prompt, sends it to ChatGPT
    via browser automation, and returns an OpenAI-formatted response.
    Supports tool/function calling via prompt injection.
    """
    # ── Validate ────────────────────────────────────────────
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported. Set stream=false or omit it.",
        )

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")

    provider = _resolve_provider(request.model, path_provider)
    spec = get_provider(provider)
    if not spec.supports_chat:
        raise HTTPException(
            status_code=501,
            detail=(
                f"{spec.label} is a media-only provider and does not support chat "
                f"completions. Use its media endpoint (e.g. /v1/videos/generations)."
            ),
        )
    # Which of the provider's UI models serves this request (Gemini/Grok expose
    # several; "" keeps whatever the account has selected).
    chat_model = _resolve_chat_model(provider, request.model)

    async with _get_pool().acquire(
        provider=provider,
        model=chat_model or request.model or "",
    ) as worker:
        client = worker.client
        start_time = time.time()

        # ── Build the prompt ────────────────────────────────
        messages = list(request.messages)

        # If tools are provided, inject tool definitions as a system prompt
        # (unless tool_choice="none", which means ignore tools)
        has_tool_prompt = False
        if request.tools and request.tool_choice != "none":
            tool_system = _build_tool_system_prompt(
                request.tools, tool_choice=request.tool_choice
            )
            # Prepend as the first system message
            messages.insert(0, ChatMessage(role="system", content=tool_system))
            has_tool_prompt = True

        prompt = _build_prompt(messages)
        log.info(
            f"POST /v1/chat/completions — model={request.model}, "
            f"{len(request.messages)} messages, prompt={len(prompt)} chars"
            f"{f', ui_model={chat_model}' if chat_model else ''}"
        )

        # ── Extract attachments from messages ──────────────
        image_paths: list[str] = []
        file_paths: list[str] = []
        for msg in request.messages:
            if msg.role == "user" and isinstance(msg.content, list):
                # Images (OpenAI vision format)
                image_urls = _extract_image_urls(msg.content)
                for url in image_urls:
                    local_path = await _download_file(url)
                    if local_path:
                        image_paths.append(local_path)
                # Generic file attachments
                file_attachments = _extract_file_attachments(msg.content)
                for fa in file_attachments:
                    local_path = await _download_file(fa)
                    if local_path:
                        file_paths.append(local_path)

        all_attachment_paths = image_paths + file_paths
        if all_attachment_paths:
            log.info(f"Extracted {len(image_paths)} image(s) and {len(file_paths)} file(s) from request")

        # Start a fresh conversation to avoid thread exhaustion
        await worker.ensure_fresh_chat()

        # ── Send to ChatGPT ────────────────────────────────
        try:
            result = await client.send_message(
                prompt,
                image_paths=image_paths or None,
                file_paths=file_paths or None,
                chat_model=chat_model,
            )
        except Exception as e:
            log.error(f"Provider error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Provider error: {str(e)}")

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)

        # ── Detect echo (extraction grabbed sent prompt instead of reply) ──
        if response_text and has_tool_prompt and any(m in response_text for m in _ECHO_MARKERS):
            log.warning("Response appears to echo the sent prompt — retrying extraction")
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.providers.claude.detector import extract_last_response_via_copy
                else:
                    from src.providers.chatgpt.detector import extract_last_response_via_copy
                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(m in retry_text for m in _ECHO_MARKERS):
                    response_text = retry_text
                    log.info(f"Retry extraction succeeded: {len(response_text)} chars")
                else:
                    log.warning("Retry extraction still echoed — stripping system prefix")
                    # Last resort: try to find assistant content after the prompt
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        finish_reason = "stop"

        if has_tool_prompt and request.tools:
            tool_calls = _parse_tool_calls(response_text, request.tools)
            if tool_calls:
                finish_reason = "tool_calls"
                # When the model calls tools, content should be null
                response_text = None

        # ── Build response ──────────────────────────────────
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text or "")

        response = ChatCompletionResponse(
            model=request.model,
            choices=[
                Choice(
                    index=0,
                    message=ChoiceMessage(
                        role="assistant",
                        content=response_text,
                        tool_calls=tool_calls,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

        log.info(
            f"Response[{worker.account_id}]: {elapsed_ms}ms, finish_reason={finish_reason}, "
            f"tokens≈{response.usage.total_tokens}"
        )

        await _observe(worker, result)
        worker.increment_thread_count()
        return response


# ── Responses API (/v1/responses) ───────────────────────────────


def _extract_responses_input_images(input_data: str | list) -> list[str]:
    """Collect reference-image specs from a Responses API `input` array.

    Looks inside every user item's content parts for image parts (input_image /
    image_url, string or nested). A plain-string input carries no images.
    Returns data-URL / http-URL strings, in order.
    """
    if not isinstance(input_data, list):
        return []
    specs: list[str] = []
    for item in input_data:
        if not isinstance(item, dict):
            continue
        # Only user-supplied content carries reference images.
        if item.get("role") not in (None, "user"):
            continue
        content = item.get("content")
        if isinstance(content, list):
            specs.extend(_extract_image_urls(content))
    return specs


def _responses_input_to_messages(
    input_data: str | list,
    instructions: str | None = None,
) -> list[ChatMessage]:
    """
    Convert Responses API `input` (string or item array) into a list of
    ChatMessage objects compatible with our existing _build_prompt().

    Handles:
      - Plain string → single user message
      - Array of message objects (role + content)
      - function_call items (assistant requested a tool)
      - function_call_output items (tool results)
    """
    messages: list[ChatMessage] = []

    # System prompt from `instructions`
    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    # Simple string input
    if isinstance(input_data, str):
        messages.append(ChatMessage(role="user", content=input_data))
        return messages

    # Array of items
    for item in input_data:
        if isinstance(item, str):
            messages.append(ChatMessage(role="user", content=item))
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role")

        if item_type == "function_call":
            # Assistant called a tool — record as assistant message with tool_calls
            name = item.get("name", "")
            arguments = item.get("arguments", "{}")
            call_id = item.get("call_id", f"call_{uuid.uuid4().hex[:24]}")
            messages.append(
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id=call_id,
                            type="function",
                            function=FunctionCallInfo(
                                name=name, arguments=arguments
                            ),
                        )
                    ],
                )
            )
        elif item_type == "function_call_output":
            # Tool result — map to role=tool
            call_id = item.get("call_id", "")
            output = item.get("output", "")
            messages.append(
                ChatMessage(
                    role="tool",
                    content=output,
                    tool_call_id=call_id,
                )
            )
        elif item_type == "message" or role:
            # Regular message item
            r = role or item.get("role", "user")
            # Map "developer" role to "system"
            if r == "developer":
                r = "system"
            content = item.get("content", "")
            # Content can be a list of content parts or a string
            if isinstance(content, list):
                # Extract text from content parts
                text_parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "input_text":
                            text_parts.append(part.get("text", ""))
                        elif part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts) if text_parts else ""
            messages.append(ChatMessage(role=r, content=content))

    return messages


def _responses_tools_to_chat_tools(
    tools: list[dict],
) -> list[ToolDefinition]:
    """
    Convert flat Responses API tool definitions to nested Chat Completions
    ToolDefinition format so we can reuse _build_tool_system_prompt().

    Responses:  {"type": "function", "name": "X", "parameters": {...}}
    Chat:       {"type": "function", "function": {"name": "X", "parameters": {...}}}
    """
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            tool = tool.model_dump() if hasattr(tool, "model_dump") else dict(tool)
        if tool.get("type") != "function":
            continue
        result.append(
            ToolDefinition(
                type="function",
                function=FunctionDefinition(
                    name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {}),
                ),
            )
        )
    return result


def _build_response_object(
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
    request: "ResponsesRequest",
    prompt_tokens: int,
    completion_tokens: int,
    images: list | None = None,
    tools_echo: list | None = None,
) -> ResponseObject:
    """Build a full ResponseObject from the model output."""
    now = int(time.time())
    output: list = []
    output_text_val: str | None = None

    # Generated images become image_generation_call items, which is where the
    # Responses API puts them. Emitted before any text so a client reading
    # output[0] finds the image.
    for img_b64 in images or []:
        output.append(
            {
                "type": "image_generation_call",
                "id": f"ig_{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "result": img_b64,
            }
        )

    if tool_calls:
        for tc in tool_calls:
            output.append(
                ResponseFunctionCall(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                    call_id=tc.id,
                ).model_dump()
            )
    else:
        text = response_text or ""
        # An image turn carries no real assistant text — the scraper picks up the
        # "Edit" button that ChatGPT overlays on generated images. Emitting that
        # as the model's reply would be nonsense, so drop it.
        if images and text.strip().lower() in ("", "edit"):
            output_text_val = None
        else:
            msg = ResponseOutputMessage(
                content=[ResponseOutputText(text=text)]
            )
            output.append(msg.model_dump())
            output_text_val = text

    usage = ResponseUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )

    # Tool schemas were already serialized once in create_response; reuse them.
    # (Fallback keeps this correct for any caller that doesn't pass them.)
    if tools_echo is None:
        tools_echo = []
        if request.tools:
            for t in request.tools:
                tools_echo.append(
                    t.model_dump() if hasattr(t, "model_dump") else dict(t)
                )

    return ResponseObject(
        created_at=now,
        completed_at=now,
        status="completed",
        model=request.model,
        instructions=request.instructions,
        max_output_tokens=request.max_output_tokens,
        output=output,
        output_text=output_text_val,
        temperature=request.temperature,
        top_p=request.top_p,
        tool_choice=request.tool_choice or "auto",
        tools=tools_echo,
        previous_response_id=request.previous_response_id,
        usage=usage,
        metadata=request.metadata or {},
    )


async def _stream_response_events(
    resp: ResponseObject,
    response_text: str | None,
    tool_calls: list[ToolCall] | None,
):
    """
    Yield SSE events for a streaming Responses API call.

    Since the browser backend doesn't truly stream, we emit the full
    response as a burst of events matching the OpenAI SSE contract:
      response.created → response.in_progress →
      output_item.added → content_part.added →
      output_text.delta (full text as one chunk) →
      output_text.done → content_part.done →
      output_item.done → response.completed
    """
    seq = 0
    resp_dict = resp.model_dump()

    def _event(event_type: str, data: dict) -> str:
        data["type"] = event_type
        data["sequence_number"] = seq
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    # 1) response.created
    created_resp = dict(resp_dict)
    created_resp["status"] = "in_progress"
    created_resp["completed_at"] = None
    created_resp["output"] = []
    created_resp["output_text"] = None
    created_resp["usage"] = None
    yield _event("response.created", {"response": created_resp})
    seq += 1

    # 2) response.in_progress
    yield _event("response.in_progress", {"response": created_resp})
    seq += 1

    if tool_calls:
        # Emit function call output items
        for idx, tc in enumerate(tool_calls):
            fc_item = ResponseFunctionCall(
                name=tc.function.name,
                arguments=tc.function.arguments,
                call_id=tc.id,
            ).model_dump()

            # output_item.added
            fc_added = dict(fc_item)
            fc_added["status"] = "in_progress"
            yield _event("response.output_item.added", {
                "output_index": idx,
                "item": fc_added,
            })
            seq += 1

            # function_call_arguments.delta (one burst)
            yield _event("response.function_call_arguments.delta", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "delta": tc.function.arguments,
            })
            seq += 1

            # function_call_arguments.done
            yield _event("response.function_call_arguments.done", {
                "item_id": fc_item["id"],
                "output_index": idx,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
            seq += 1

            # output_item.done
            yield _event("response.output_item.done", {
                "output_index": idx,
                "item": fc_item,
            })
            seq += 1
    else:
        # Emit text message output
        text = response_text or ""
        msg = ResponseOutputMessage(
            content=[ResponseOutputText(text=text)]
        )
        msg_dict = msg.model_dump()

        # output_item.added (empty content)
        msg_added = dict(msg_dict)
        msg_added["status"] = "in_progress"
        msg_added["content"] = []
        yield _event("response.output_item.added", {
            "output_index": 0,
            "item": msg_added,
        })
        seq += 1

        # content_part.added
        yield _event("response.content_part.added", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })
        seq += 1

        # output_text.delta — full text as one chunk
        if text:
            yield _event("response.output_text.delta", {
                "item_id": msg_dict["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": text,
            })
            seq += 1

        # output_text.done
        yield _event("response.output_text.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
        })
        seq += 1

        # content_part.done
        yield _event("response.content_part.done", {
            "item_id": msg_dict["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        seq += 1

        # output_item.done
        yield _event("response.output_item.done", {
            "output_index": 0,
            "item": msg_dict,
        })
        seq += 1

    # response.completed
    yield _event("response.completed", {"response": resp_dict})


@openai_router.post("/v1/responses")
@openai_router.post("/v1/{path_provider}/responses")
@retry_across_accounts(can_retry=_can_retry_unpinned_response)
async def create_response(request: ResponsesRequest, path_provider: Optional[str] = None):
    """
    OpenAI Responses API endpoint — compatible with Codex CLI.

    Accepts the Responses API format (flat tools, `input` field, `instructions`),
    translates to our internal format, sends to the browser, and returns a
    Responses-API-shaped response (or SSE stream).
    """
    # ── Validate ────────────────────────────────────────────
    if not request.input:
        raise HTTPException(status_code=400, detail="input cannot be empty")

    # ── image_generation tool? ──────────────────────────────
    # This is not a function-calling tool: it means "produce an image", so it
    # routes to the image path instead of the text/tool-prompt path.
    raw_tools_all = [
        t.model_dump() if hasattr(t, "model_dump") else dict(t)
        for t in (request.tools or [])
    ]
    image_tool = next(
        (t for t in raw_tools_all if t.get("type") == "image_generation"), None
    )
    # `name` is optional on the schema because built-in tools do not have one;
    # for function tools it is still required, so enforce it here where the
    # type is known rather than silently registering a nameless function.
    for t in raw_tools_all:
        if t.get("type") == "function" and not t.get("name"):
            raise HTTPException(
                status_code=400,
                detail="tools[].name is required when type='function'",
            )
    if image_tool and image_tool.get("input_image_mask"):
        # Masks reference a file_id from /v1/files, which this gateway does not
        # implement — and the web UI has no inpainting surface regardless.
        log.warning("/v1/responses: ignoring input_image_mask (no inpainting via the web UI)")

    # Resolve chaining BEFORE acquiring, so the continuation is PINNED to the
    # account that owns the thread (no other account can open that /c/<id>).
    chain = _resolve_chain(request.previous_response_id) if request.previous_response_id else None
    pinned_account = chain[0] if chain else None

    provider = _resolve_provider(request.model, path_provider)
    if not get_provider(provider).supports_chat:
        raise HTTPException(
            status_code=501,
            detail=(
                f"{get_provider(provider).label} is a media-only provider and does "
                f"not support the Responses API. Use its media endpoint."
            ),
        )
    chat_model = _resolve_chat_model(provider, request.model)

    async with _acquire_for_response(
        pinned_account,
        chain,
        provider,
        model=chat_model or request.model or "",
    ) as worker:
        client = worker.client
        start_time = time.time()

        # ── Convert input to ChatMessage list ───────────────
        messages = _responses_input_to_messages(
            request.input, instructions=request.instructions
        )

        # ── Convert flat tools to nested format ─────────────
        chat_tools: list[ToolDefinition] | None = None
        has_tool_prompt = False
        if request.tools and not image_tool:
            raw_tools = raw_tools_all
            chat_tools = _responses_tools_to_chat_tools(raw_tools)
            if chat_tools and request.tool_choice != "none":
                tool_system = _build_tool_system_prompt(
                    chat_tools, tool_choice=request.tool_choice
                )
                messages.insert(
                    0, ChatMessage(role="system", content=tool_system)
                )
                has_tool_prompt = True

        prompt = _build_prompt(messages)
        if image_tool:
            # Nudge the UI into generating rather than describing. On a chained
            # turn ("make the sky bluer") the thread already holds the previous
            # image, so this reads as an edit.
            prompt = _build_image_prompt(
                prompt if request.previous_response_id else f"Generate an image: {prompt}",
                quality=image_tool.get("quality") or "auto",
                background=image_tool.get("background") or "auto",
                size=image_tool.get("size") or "auto",
            )

        # ── Reference images from the input ─────────────────
        # Any image parts in the user input are uploaded to ChatGPT alongside
        # the prompt as visual context (for both text answers and image tools).
        ref_paths = await _download_reference_images(
            _extract_responses_input_images(request.input), endpoint="/v1/responses"
        )
        if ref_paths and image_tool:
            prompt = f"Using the attached image(s) as reference, {prompt}"

        log.info(
            f"POST /v1/responses — model={request.model}, "
            f"input_type={'string' if isinstance(request.input, str) else 'array'}, "
            f"prompt={len(prompt)} chars, stream={request.stream}, "
            f"image_tool={bool(image_tool)}, "
            f"reference_images={len(ref_paths)}, "
            f"chained={bool(request.previous_response_id)}"
        )

        # ── Conversation chaining ───────────────────────────
        # A chained request resumes its thread on the pinned account and
        # suppresses rotation (which would start a new chat and drop the context).
        if chain:
            await _resume_thread(worker, chain[0], chain[1])
        await worker.ensure_fresh_chat(keep_thread=bool(chain))

        # ── Send to browser ────────────────────────────────
        # expect_image makes the detector wait for a rendered <img> instead of a
        # copy button — image turns never grow one.
        try:
            result = await client.send_message(prompt, image_paths=ref_paths or None, expect_image=bool(image_tool), chat_model=chat_model)
        except RuntimeError as e:
            err_msg = str(e).lower()
            if "error state" in err_msg or "could not find chat input" in err_msg:
                # Page has a DNS/navigation error or UI is broken — attempt recovery
                log.warning(f"Page error detected, attempting recovery: {e}")
                if await worker.recover():
                    # Retry after recovery
                    try:
                        result = await client.send_message(prompt, image_paths=ref_paths or None, expect_image=bool(image_tool), chat_model=chat_model)
                    except Exception as e2:
                        log.error(f"Provider error after recovery: {e2}", exc_info=True)
                        raise HTTPException(
                            status_code=500, detail=f"Provider error: {str(e2)}"
                        )
                else:
                    raise HTTPException(
                        status_code=503, detail="Browser page is in error state and recovery failed"
                    )
            else:
                log.error(f"Provider error: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail=f"Provider error: {str(e)}"
                )
        except Exception as e:
            err_name = type(e).__name__
            # TargetClosedError means browser/page crashed — try recovery
            if "TargetClosed" in err_name or "closed" in str(e).lower():
                log.warning(f"Browser/page crashed ({err_name}), attempting recovery...")
                if await worker.recover():
                    try:
                        result = await client.send_message(prompt, image_paths=ref_paths or None, expect_image=bool(image_tool), chat_model=chat_model)
                    except Exception as e2:
                        log.error(f"Provider error after crash recovery: {e2}", exc_info=True)
                        raise HTTPException(
                            status_code=500, detail=f"Provider error: {str(e2)}"
                        )
                else:
                    raise HTTPException(
                        status_code=503, detail=f"Browser crashed and recovery failed: {err_name}"
                    )
            else:
                log.error(f"Provider error: {e}", exc_info=True)
                raise HTTPException(
                    status_code=500, detail=f"Provider error: {str(e)}"
                )

        response_text = result.message
        elapsed_ms = int((time.time() - start_time) * 1000)
        _cleanup_paths(ref_paths)  # reference images are uploaded by now

        # ── Detect echo ────────────────────────────────────
        if (
            response_text
            and has_tool_prompt
            and any(m in response_text for m in _ECHO_MARKERS)
        ):
            log.warning(
                "Response appears to echo the sent prompt — retrying extraction"
            )
            try:
                await asyncio.sleep(1.5)
                if Config.PROVIDER == "claude":
                    from src.providers.claude.detector import extract_last_response_via_copy
                else:
                    from src.providers.chatgpt.detector import extract_last_response_via_copy

                retry_text = await extract_last_response_via_copy(client.page)
                if retry_text and not any(
                    m in retry_text for m in _echo_markers
                ):
                    response_text = retry_text
                    log.info(
                        f"Retry extraction succeeded: {len(response_text)} chars"
                    )
                else:
                    log.warning(
                        "Retry extraction still echoed — stripping system prefix"
                    )
                    idx = response_text.rfind("\n\n")
                    if idx > 0:
                        tail = response_text[idx:].strip()
                        if tail and not tail.startswith("["):
                            response_text = tail
            except Exception as e:
                log.warning(f"Retry extraction failed: {e}")

        # ── Check for tool calls ────────────────────────────
        tool_calls = None
        if has_tool_prompt and chat_tools:
            tool_calls = _parse_tool_calls(response_text, chat_tools)
            if tool_calls:
                response_text = None

        # ── Collect generated images ────────────────────────
        image_b64: list[str] = []
        if image_tool:
            if not result.images:
                log.warning(
                    f"image_generation requested but no image was produced. "
                    f"ChatGPT replied: {(result.message or '')[:200]}"
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{get_provider(worker.provider).label} did not generate an image. Model response: "
                        f"{(result.message or '')[:500]}"
                    ),
                )
            for img_info in result.images:
                if not img_info.local_path:
                    continue
                try:
                    raw = Path(img_info.local_path).read_bytes()
                except Exception as e:
                    log.error(f"Failed to read generated image: {e}")
                    continue
                # Off the event loop — see _run_image_request.
                converted, _fmt = await asyncio.to_thread(
                    _convert_image,
                    raw,
                    image_tool.get("output_format") or "png",
                    image_tool.get("output_compression"),
                )
                image_b64.append(base64.b64encode(converted).decode("utf-8"))

        # ── Build response ──────────────────────────────────
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(response_text or "")

        resp = _build_response_object(
            response_text, tool_calls, request,
            prompt_tokens, completion_tokens,
            images=image_b64,
            tools_echo=raw_tools_all,
        )

        # Remember (account, thread) for this response, so a follow-up carrying
        # previous_response_id=<this id> resumes the SAME account's conversation.
        # Only after the /c/<id> URL has materialized (extract can return "").
        tid = worker.extract_thread_id()
        if tid:
            _remember_response_thread(resp.id, worker.account_id, tid)

        log.info(
            f"Response[{worker.account_id}]: {elapsed_ms}ms, "
            f"tool_calls={len(tool_calls) if tool_calls else 0}, "
            f"images={len(image_b64)}, "
            f"tokens≈{resp.usage.total_tokens if resp.usage else 0}"
        )

        await _observe(worker, result)
        worker.increment_thread_count()

        # ── Stream or return ────────────────────────────────
        if request.stream:
            return StreamingResponse(
                _stream_response_events(resp, response_text, tool_calls),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return resp.model_dump()
