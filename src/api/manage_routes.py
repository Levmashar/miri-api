"""
Admin / control-plane API + the /admin panel shell.

GET /admin serves a self-contained HTML page (no secrets, no external CDN) — it
is in the middleware's OPEN_PATHS. Every /admin/api/* endpoint under it is
key-gated by require_admin: it needs ADMIN_TOKEN (or API_TOKEN if ADMIN_TOKEN is
unset) in an Authorization: Bearer header, and HARD-REFUSES (403) if neither
token is configured — the control plane must never run wide open, because it can
delete accounts and drive logged-in browsers.

The panel is also injectable into the noVNC page (see docker/novnc_panel.js),
which calls these same endpoints cross-origin (CORS is already open) with the
admin token the operator pastes once.
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from src.accounts.registry import AccountConfig, validate_id
from src.providers.base import ENABLED_PROVIDER_IDS, get_provider
from src.core.config import Config
from src.core.log import setup_logging
from src.core import resources
from src.core.runtime_settings import RuntimeSettings

log = setup_logging("manage_routes")

manage_router = APIRouter()

# Injected by server.py at startup.
_mgr = None
_registry = None
_usage = None
_pool = None
_proxies = None
_req_log = None
_settings: RuntimeSettings | None = None


def set_manage_context(manager, registry, usage, pool, proxies=None, settings=None) -> None:
    global _mgr, _registry, _usage, _pool, _proxies, _settings
    _mgr, _registry, _usage, _pool, _proxies = manager, registry, usage, pool, proxies
    _settings = settings


def set_request_log(store) -> None:
    global _req_log
    _req_log = store


def _require_admin(request: Request) -> None:
    """Gate a control-plane endpoint. 403 if no token configured; 401 if wrong."""
    required = Config.ADMIN_TOKEN or Config.API_TOKEN
    if not required:
        raise HTTPException(
            status_code=403,
            detail="Control plane is disabled: set ADMIN_TOKEN (or API_TOKEN) to use /admin.",
        )
    auth = request.headers.get("authorization", "")
    provided = auth[7:] if auth.startswith("Bearer ") else ""
    if not (provided and secrets.compare_digest(provided, required)):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


def _ready():
    if _mgr is None or _pool is None:
        raise HTTPException(status_code=503, detail="Server not initialized")


def _account_view(acc) -> dict:
    """Serialized account: config (redacted) + live state + usage + assigned proxy."""
    snap = _usage.snapshot(acc.cfg.id) if _usage else {}
    # Tolerate an unknown provider (e.g. a hand-edited accounts.json) so one bad
    # row can never 500 the whole /admin status render.
    try:
        provider_label = get_provider(acc.cfg.provider).label
    except Exception:
        provider_label = acc.cfg.provider or "?"
    return {
        **acc.cfg.redacted(),
        "provider_label": provider_label,
        "state": acc.state.value,
        "started": acc.started,
        "tabs_live": len(acc.workers),
        "auto_promoted": acc.auto_promoted,
        "last_error": acc.last_error,
        "assigned_proxy": _mgr.proxy_label(acc.cfg) if _mgr else "",
        "usage": snap,
    }


# ── The panel shell ──────────────────────────────────────────────

@manage_router.get("/admin", include_in_schema=False)
async def admin_page() -> HTMLResponse:
    from src.api.admin_html import ADMIN_HTML
    return HTMLResponse(ADMIN_HTML)


# ── Control-plane API (all require_admin) ───────────────────────

@manage_router.get("/admin/api/status")
async def admin_status(request: Request):
    _require_admin(request)
    _ready()
    return {
        "provider": Config.PROVIDER,
        "settings": (_settings.snapshot() if _settings else {
            "new_chat_every_request": Config.NEW_CHAT_EVERY_REQUEST,
            "temporary_chats": Config.TEMPORARY_CHATS,
        }),
        "tabs_total": _pool.size(),
        "tabs_schedulable": _pool.schedulable_size(),
        "tabs_idle": _pool.idle_count(),
        "queue_depth": _pool.queue_depth(),
        "saturated": _pool.saturated(),
        "vnc_url": Config.VNC_PUBLIC_URL,
        "proxy_count": _proxies.count() if _proxies else 0,
        # CPU/RAM of this container (or host) — the browsers are what fill it,
        # so the panel draws a bar and calls out their share. Never raises;
        # {"available": false} when nothing could be read.
        "resources": resources.snapshot(),
        # Providers run simultaneously; each is an independent capacity pool, so
        # the UI shows a tab per provider with its own tabs/idle/saturation.
        "providers": [
            {
                "id": pid,
                "label": get_provider(pid).label,
                "supports_chat": get_provider(pid).supports_chat,
                "supports_images": get_provider(pid).supports_images,
                "supports_video": get_provider(pid).supports_video,
                "chat_model": get_provider(pid).chat_model_id,
                # Every chat model this provider's web UI can be pointed at
                # (Gemini/Grok expose several; the rest just their default).
                "chat_models": list(
                    get_provider(pid).chat_models or
                    ((get_provider(pid).chat_model_id,) if get_provider(pid).chat_model_id else ())
                ),
                "image_model": get_provider(pid).image_model_id,
                "video_model": get_provider(pid).video_model_id,
                **_pool.provider_stats(pid),
                "saturated": _pool.saturated_for(pid),
            }
            for pid in ENABLED_PROVIDER_IDS
        ],
        "accounts": [_account_view(a) for a in _mgr.all()],
    }


@manage_router.patch("/admin/api/settings")
async def update_settings(request: Request):
    """Update request chat policy and apply it to new requests immediately."""
    _require_admin(request)
    if _settings is None:
        raise HTTPException(status_code=503, detail="settings not ready")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="settings body must be an object")
    try:
        return {"settings": await _settings.update(body)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Proxy pool ──────────────────────────────────────────────────

@manage_router.get("/admin/api/proxies")
async def get_proxies(request: Request):
    """The proxy list as editable text (one per line).

    Returns the FULL lines including credentials — this is an admin-only surface
    (already gated by the admin token) and the operator must be able to edit them.
    """
    _require_admin(request)
    if _proxies is None:
        raise HTTPException(status_code=503, detail="proxy pool not ready")
    return {
        "text": "\n".join(_proxies.raw_lines()),
        "count": _proxies.count(),
        "note": ("account N uses proxy ((N-1) mod count)+1; fewer proxies than "
                 "accounts wraps back to proxy 1. Restart an account to apply a change."),
    }


@manage_router.put("/admin/api/proxies")
async def put_proxies(request: Request):
    """Replace the whole proxy list from the textarea. Validates every line."""
    _require_admin(request)
    if _proxies is None:
        raise HTTPException(status_code=503, detail="proxy pool not ready")
    body = await request.json()
    text = body.get("text", "")
    lines = [ln for ln in text.replace("\r", "").split("\n")]
    try:
        await _proxies.replace(lines)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"count": _proxies.count(), "proxies": _proxies.redacted_lines()}


@manage_router.post("/admin/api/accounts")
async def create_account(request: Request):
    _require_admin(request)
    _ready()
    body = await request.json()
    try:
        provider = (body.get("provider") or "chatgpt").lower()
        get_provider(provider)  # validate; raises ValueError -> 400
        # Id and timezone are AUTO now: the UI no longer asks for them. An
        # explicit id is still honored (API callers may want a stable name);
        # otherwise mint a provider-prefixed one. Timezone defaults to "auto"
        # (geo-detect from the account's proxy exit at launch).
        raw_id = (body.get("id") or "").strip()
        aid = validate_id(raw_id) if raw_id else _registry.suggest_id(provider)
        cfg = AccountConfig(
            id=aid,
            provider=provider,
            label=body.get("label", "") or aid,
            enabled=bool(body.get("enabled", True)),
            order=int(body.get("order", 100)),
            number=int(body.get("number", 0)),   # 0 = auto-assign next
            tabs=int(body.get("tabs", 0)),
            soft_cap=int(body.get("soft_cap", 0)),
            proxy_server=body.get("proxy_server", "") or "",
            proxy_username=body.get("proxy_username", "") or "",
            proxy_password=body.get("proxy_password", "") or "",
            proxy_bypass=body.get("proxy_bypass", "localhost,127.0.0.1,::1"),
            timezone=(body.get("timezone") or "auto"),
        )
        # Validate the proxy now (fail fast) if one was given.
        if cfg.proxy_server:
            Config.build_proxy(cfg.proxy_server, cfg.proxy_username, cfg.proxy_password, cfg.proxy_bypass)
        acc = await _mgr.create_account(cfg)
        return _account_view(acc)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@manage_router.patch("/admin/api/accounts/{account_id}")
async def update_account(account_id: str, request: Request):
    _require_admin(request)
    _ready()
    body = await request.json()
    allowed = {"label", "provider", "enabled", "order", "number", "tabs", "soft_cap",
               "proxy_server", "proxy_username", "proxy_password",
               "proxy_bypass", "timezone"}
    changes = {k: v for k, v in body.items() if k in allowed}
    try:
        # Validate/normalise a provider change before it is persisted — an
        # unknown value would otherwise be written and then 500 every /status
        # render (get_provider raises in _account_view).
        if "provider" in changes:
            changes["provider"] = str(changes["provider"]).lower()
            get_provider(changes["provider"])  # raises ValueError -> 400
        if changes.get("proxy_server"):
            Config.build_proxy(
                changes.get("proxy_server", ""),
                changes.get("proxy_username", ""),
                changes.get("proxy_password", ""),
                changes.get("proxy_bypass", "localhost,127.0.0.1,::1"),
            )
        await _mgr.update_account(account_id, **changes)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    acc = _mgr.get(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="no such account")
    return _account_view(acc)


@manage_router.delete("/admin/api/accounts/{account_id}")
async def delete_account(account_id: str, request: Request):
    _require_admin(request)
    _ready()
    try:
        await _mgr.remove_account(account_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"removed": account_id}


@manage_router.post("/admin/api/accounts/{account_id}/start")
async def start_account(account_id: str, request: Request):
    _require_admin(request)
    _ready()
    acc = _mgr.get(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="no such account")
    await _mgr.start_account(account_id)
    return _account_view(_mgr.get(account_id))


@manage_router.post("/admin/api/accounts/{account_id}/stop")
async def stop_account(account_id: str, request: Request):
    _require_admin(request)
    _ready()
    await _mgr.stop_account(account_id)
    return _account_view(_mgr.get(account_id))


@manage_router.post("/admin/api/accounts/{account_id}/login-open")
async def login_open(account_id: str, request: Request):
    """Open the provider's login page on this account's tab (starts it if needed).

    Suspends routing to the account while you sign in by hand via noVNC.
    """
    _require_admin(request)
    _ready()
    if _mgr.get(account_id) is None:
        raise HTTPException(status_code=404, detail="no such account")
    await _mgr.open_login(account_id)
    return _account_view(_mgr.get(account_id))


@manage_router.post("/admin/api/accounts/{account_id}/logout")
async def logout_account(account_id: str, request: Request):
    """Sign the account out (clears cookies + storage). Browser stays open."""
    _require_admin(request)
    _ready()
    if _mgr.get(account_id) is None:
        raise HTTPException(status_code=404, detail="no such account")
    await _mgr.logout(account_id)
    return _account_view(_mgr.get(account_id))


@manage_router.post("/admin/api/accounts/{account_id}/focus")
async def focus_account(account_id: str, request: Request):
    """Raise this account's browser window in the viewer.

    Every account's Chrome window shares the one X display, so they overlap.
    Bringing a page to front is what makes the viewer show THAT account. Pass
    ?tab=N to surface a specific tab of the account.
    """
    _require_admin(request)
    _ready()
    acc = _mgr.get(account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail="no such account")
    if not acc.started or not acc.workers:
        raise HTTPException(status_code=409, detail="account is not started")
    try:
        tab = int(request.query_params.get("tab", "0"))
    except ValueError:
        tab = 0
    tab = max(0, min(tab, len(acc.workers) - 1))
    try:
        await acc.workers[tab].page.bring_to_front()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"could not focus: {e}")
    return {"account_id": account_id, "tab": tab, "tabs": len(acc.workers)}


@manage_router.post("/admin/api/accounts/{account_id}/login-confirm")
async def login_confirm(account_id: str, request: Request):
    _require_admin(request)
    _ready()
    ok = await _mgr.confirm_login(account_id)
    return {"account_id": account_id, "logged_in": ok, "state": _mgr.get(account_id).state.value}


# ── Request log (the admin Logs viewer) ─────────────────────────

@manage_router.get("/admin/api/logs")
async def list_logs(request: Request):
    """Newest-first summaries of the last N API requests."""
    _require_admin(request)
    if _req_log is None:
        raise HTTPException(status_code=503, detail="request log not ready")
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1000))
    try:
        before = int(request.query_params['before']) if request.query_params.get('before') else None
    except ValueError:
        raise HTTPException(status_code=400, detail='Invalid log cursor')
    return _req_log.page(limit, before=before, query=request.query_params.get('q', '')[:200])


# Registered BEFORE /admin/api/logs/{log_id}: that route declares an int, so
# "export" would be matched by it and rejected as a bad id instead of landing here.
@manage_router.get("/admin/api/logs/export")
async def export_logs(request: Request):
    """Download the last N requests IN FULL, plus (optionally) the server logs.

    The Logs table shows one line per request and the per-request bodies only
    one click at a time, which is unworkable when something needs to be handed
    to someone else. This returns one attachment holding everything.

    Query params: limit (1..5000), q (same filter as the table), files=0|1 to
    embed the tail of each file in LOG_DIR.
    """
    _require_admin(request)
    if _req_log is None:
        raise HTTPException(status_code=503, detail="request log not ready")

    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 5000))
    query = request.query_params.get("q", "")[:200]
    include_files = request.query_params.get("files", "1") not in ("0", "false", "no")

    entries = _req_log.export(limit, query=query)
    payload = {
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "exported_at_epoch": time.time(),
        "filter": query,
        "request_log": {
            "requested": limit,
            "returned": len(entries),
            "stored": _req_log.count(),
            "entries": entries,
        },
    }
    if include_files:
        payload["server_logs"] = _tail_log_files()
        payload["server_logs_note"] = (
            "Tail of each file in LOG_DIR. One request writes to SEVERAL of these "
            "(e.g. a Qwen video touches qwen_client, qwen_detector, qwen_media_mode, "
            "worker_pool and openai_routes), which is why reading only one of them "
            "looks like missing logs."
        )

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="miri-logs-{stamp}.json"',
            "Cache-Control": "no-store",
        },
    )


# Per file, and across all of them: a download has to stay openable.
_TAIL_PER_FILE = 512 * 1024
_TAIL_TOTAL = 24 * 1024 * 1024


def _tail_log_files() -> dict:
    """Last bytes of every *.log in LOG_DIR, newest file first."""
    out: dict = {}
    budget = _TAIL_TOTAL
    try:
        files = sorted(Config.LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as error:
        return {"_error": f"could not list {Config.LOG_DIR}: {error}"}
    for path in files:
        if budget <= 0:
            out[path.name] = {"skipped": "export size limit reached"}
            continue
        try:
            size = path.stat().st_size
            take = min(_TAIL_PER_FILE, budget)
            with path.open("rb") as fh:
                if size > take:
                    fh.seek(size - take)
                raw = fh.read(take)
        except OSError as error:
            out[path.name] = {"error": str(error)}
            continue
        budget -= len(raw)
        out[path.name] = {
            "bytes": size,
            "truncated": size > len(raw),
            "modified": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "text": raw.decode("utf-8", "replace"),
        }
    return out


@manage_router.get("/admin/api/logs/{log_id}")
async def get_log(log_id: int, request: Request):
    """Full detail (headers + full captured request & response bodies)."""
    _require_admin(request)
    if _req_log is None:
        raise HTTPException(status_code=503, detail="request log not ready")
    entry = _req_log.get(log_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="No such log entry — it may have expired or been cleared.",
        )
    return entry


@manage_router.delete("/admin/api/logs")
async def clear_logs(request: Request):
    """Empty the log buffer."""
    _require_admin(request)
    if _req_log is None:
        raise HTTPException(status_code=503, detail="request log not ready")
    await _req_log.clear()
    return {"cleared": True}
