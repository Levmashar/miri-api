"""
API routes — FastAPI router for ChatGPT interaction.

Endpoints:
  POST /chat              Send a message in the current/new thread
  POST /thread/{id}/chat  Send a message in a specific thread
  POST /thread/new        Start a new conversation
  GET  /threads           List recent threads
  GET  /status            Health check + login status
"""

from __future__ import annotations

from collections import OrderedDict

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    ChatRequest,
    ChatResponse,
    ImageInfoResponse,
    StatusResponse,
    ThreadInfo,
    ThreadListResponse,
)
from src.core.browser.manager import BrowserManager
from src.providers.chatgpt.client import ChatGPTClient
from src.providers.claude.client import ClaudeClient
from src.providers.temporary_chat import fresh_chat_required
from src.api.failover import retry_across_accounts
from src.core.config import Config
from src.providers.base import get_provider
from src.core.log import setup_logging

log = setup_logging("api_routes")

router = APIRouter()

# Concurrency is handled by the shared MultiAccountPool (src/api/worker_pool.py),
# which this router and the OpenAI router both use. A request checks out one
# worker (its own tab of one account) and returns it; two requests never share a
# tab and a tab never crosses accounts.
_pool = None
_thread_accounts: "OrderedDict[str, str]" = OrderedDict()
_MAX_TRACKED_THREADS = 500


def set_pool(pool) -> None:
    """Called by server.py to inject the worker pool."""
    global _pool
    _pool = pool


def _get_pool():
    if _pool is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return _pool


def _remember_thread_owner(thread_id: str, account_id: str) -> None:
    """Keep known legacy threads on the browser profile that created them."""
    if not thread_id or not account_id:
        return
    _thread_accounts[thread_id] = account_id
    _thread_accounts.move_to_end(thread_id)
    while len(_thread_accounts) > _MAX_TRACKED_THREADS:
        _thread_accounts.popitem(last=False)


def _build_response(result) -> ChatResponse:
    """Convert internal ChatResponse to API ChatResponse with image data."""
    images = [
        ImageInfoResponse(
            url=img.url,
            alt=img.alt,
            local_path=img.local_path,
            prompt_title=img.prompt_title,
        )
        for img in (result.images or [])
    ]
    return ChatResponse(
        message=result.message,
        thread_id=result.thread_id,
        response_time_ms=result.response_time_ms,
        images=images,
        has_images=result.has_images,
    )


# ── Chat ────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
@retry_across_accounts()
async def chat(req: ChatRequest) -> ChatResponse:
    """Send a message in the current conversation."""
    log.info(f"POST /chat — {len(req.message)} chars")

    provider = Config.DEFAULT_PROVIDER
    async with _get_pool().acquire(
        provider=provider,
        model=get_provider(provider).chat_model_id,
    ) as worker:
        try:
            result = await worker.client.send_message(req.message)
            worker.increment_thread_count()
            _remember_thread_owner(result.thread_id, worker.account_id)
            return _build_response(result)
        except Exception as e:
            log.error(f"Chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/thread/{thread_id}/chat", response_model=ChatResponse)
async def chat_in_thread(thread_id: str, req: ChatRequest) -> ChatResponse:
    """Send a message in a specific thread. Navigates to it first."""
    log.info(f"POST /thread/{thread_id}/chat — {len(req.message)} chars")

    if fresh_chat_required():
        raise HTTPException(
            status_code=409,
            detail=("New-chat-per-request mode cannot resume saved threads. "
                    "Send the context in the request instead."),
        )

    provider = Config.DEFAULT_PROVIDER
    async with _get_pool().acquire(
        account_id=_thread_accounts.get(thread_id),
        provider=provider,
        model=get_provider(provider).chat_model_id,
    ) as worker:
        try:
            # Navigate to the thread if not already there
            if worker.extract_thread_id() != thread_id:
                await worker.navigate_to_thread(thread_id)

            result = await worker.client.send_message(req.message)
            worker.increment_thread_count()
            _remember_thread_owner(result.thread_id, worker.account_id)
            return _build_response(result)
        except Exception as e:
            log.error(f"Thread chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/thread/new", response_model=ChatResponse)
@retry_across_accounts()
async def new_thread(req: ChatRequest) -> ChatResponse:
    """Start a new conversation and send the first message."""
    log.info(f"POST /thread/new — {len(req.message)} chars")

    provider = Config.DEFAULT_PROVIDER
    async with _get_pool().acquire(
        provider=provider,
        model=get_provider(provider).chat_model_id,
    ) as worker:
        try:
            # The pool already opened the fresh chat for this request when a
            # request-wide policy is active. Avoid opening it twice (important
            # for native temporary-chat entry flows).
            if not fresh_chat_required():
                await worker.client.new_chat()
                worker.thread_message_count = 0
            result = await worker.client.send_message(req.message)
            worker.increment_thread_count()
            _remember_thread_owner(result.thread_id, worker.account_id)
            return _build_response(result)
        except Exception as e:
            log.error(f"New thread error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ── Threads ─────────────────────────────────────────────────────


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads() -> ThreadListResponse:
    """List recent conversation threads from the sidebar."""
    log.info("GET /threads")

    provider = Config.DEFAULT_PROVIDER
    async with _get_pool().acquire(
        provider=provider,
        model=get_provider(provider).chat_model_id,
    ) as worker:
        try:
            raw_threads = await worker.client.list_threads()
            for thread in raw_threads:
                _remember_thread_owner(thread.get("id", ""), worker.account_id)
            threads = [
                ThreadInfo(id=t["id"], title=t["title"], url=t["url"])
                for t in raw_threads
            ]
            return ThreadListResponse(threads=threads)
        except Exception as e:
            log.error(f"Threads list error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ── Status ──────────────────────────────────────────────────────


@router.get("/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    """Health check — returns login status and current thread.

    Never blocks and never drives a page that a request is already using:
    is_logged_in() queries the shared page, so calling it mid-send would race
    the in-flight request. When the browser is busy we report that instead of
    waiting (a status check that can block for minutes is not a status check).
    """
    if _pool is None:
        return StatusResponse(status="uninitialized", logged_in=False, current_thread="")

    # Probe an IDLE worker so we never drive a tab mid-request (which would
    # race that request's DOM) and never block behind one.
    worker = await _pool.try_acquire_nowait()
    if worker is None:
        return StatusResponse(status="busy", logged_in=True, current_thread="")
    try:
        logged_in = await worker.browser.is_logged_in(worker.page)
        tid = worker.extract_thread_id()
        return StatusResponse(status="ok", logged_in=logged_in, current_thread=tid)
    except Exception as e:
        log.warning(f"/status probe failed: {e}")
        return StatusResponse(status="error", logged_in=False, current_thread="")
    finally:
        await _pool.release_nowait(worker)
