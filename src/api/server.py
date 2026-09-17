"""
FastAPI server — serves ChatGPT as an API.

Launches the browser on startup, shuts it down on exit.

Usage:
    python -m src.api.server
    # or
    uvicorn src.api.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.config import Config
from src.core.janitor import janitor_loop, swept_directories
from src.api.worker_pool import BrowserBusy, AccountUnavailable, MultiAccountPool
from src.accounts.registry import AccountRegistry, registry_path
from src.accounts.usage import UsageStore, usage_path
from src.accounts.proxies import ProxyPool, proxies_path
from src.accounts.manager import AccountManager
from src.accounts.monitor import AccountMonitor
from src.api.routes import router, set_pool
from src.api.openai_routes import openai_router, set_openai_pool, set_usage
from src.api.media_files import media_router
from src.api.manage_routes import manage_router, set_manage_context, set_request_log
from src.api.request_log import RequestLogStore, RequestLogMiddleware
from src.core.runtime_settings import RuntimeSettings, settings_path
from src.core.log import setup_logging

log = setup_logging("api_server")

# Global instances — needed for lifespan + admin API
_pool: MultiAccountPool | None = None
_account_manager: AccountManager | None = None
_registry: AccountRegistry | None = None
_usage: UsageStore | None = None
_monitor: AccountMonitor | None = None
_proxies: ProxyPool | None = None
_settings: RuntimeSettings | None = None
# Persistent request/response log for the admin Logs viewer. Created at import so
# the capture middleware (added below) and the /admin/api/logs routes share it.
# Pruned by age (~a week); base64 media is stripped from stored bodies.
_request_log = RequestLogStore(
    retention_seconds=Config.LOG_RETENTION_DAYS * 86400,
    max_entries=Config.LOG_MAX_ENTRIES,
    body_cap=Config.LOG_BODY_CAP,
    db_path=Config.LOG_DIR / 'requests.sqlite3',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: driver + accounts + pool + monitor. Shutdown: close them."""
    global _pool, _account_manager, _usage, _registry, _monitor, _proxies, _settings

    _settings = RuntimeSettings(settings_path())
    await _settings.load()

    if not Config.API_TOKEN:
        log.warning("=" * 62)
        log.warning("API_TOKEN is empty — THE API IS UNAUTHENTICATED.")
        log.warning("Anyone who can reach this port can use your logged-in")
        log.warning("ChatGPT/Claude session. Set API_TOKEN in .env to fix.")
        log.warning("=" * 62)

    provider_name = "Claude" if Config.PROVIDER == "claude" else "ChatGPT"

    # ── Registry + usage (persisted under the volume) ───────────
    _registry = AccountRegistry(registry_path())
    await _registry.load()
    await _registry.ensure_default()   # non-lossy: keeps the existing root login
    _usage = UsageStore(usage_path())
    await _usage.load()
    _proxies = ProxyPool(proxies_path())
    await _proxies.load()
    if _proxies.count():
        log.info(f"Proxy pool: {_proxies.count()} prox(ies) — assigned to accounts by number")

    # ── Pool + AccountManager (one shared Playwright driver) ────
    _pool = MultiAccountPool(
        max_waiters=max(3, Config.MAX_CONCURRENT_REQUESTS * 2),
    )
    _account_manager = AccountManager(_registry, _usage, _pool, proxies=_proxies)

    async def _on_account_failure(account_id: str, reason: str) -> None:
        """Reflect a data-plane failure in persisted account state immediately."""
        if _usage is not None:
            _usage.note_failure(account_id)
        if _account_manager is not None:
            await _account_manager.mark_failed(account_id, reason)

    _pool.set_failure_handler(_on_account_failure)
    await _account_manager.start_driver()
    await _account_manager.load_accounts()

    # ── Start every enabled account up front ────────────────────
    # A persisted logged-in profile becomes schedulable after start_account()
    # verifies its session. Starting all enabled profiles makes the account-level
    # round robin immediately share traffic between every available login.
    # Logged-out profiles stay unschedulable until the user signs in via noVNC.
    started_any = False
    for acc in _account_manager.all():
        if not acc.cfg.enabled:
            continue
        await _account_manager.start_account(acc.cfg.id)
        started_any = True
        st = _account_manager.get(acc.cfg.id)
        log.info(f"[{acc.cfg.provider}] '{acc.cfg.id}' started (state={st.state})")
    if not started_any:
        log.warning("No enabled accounts to start")

    set_pool(_pool)
    set_openai_pool(_pool)
    set_usage(_usage)
    set_manage_context(_account_manager, _registry, _usage, _pool, _proxies, _settings)

    # ── Background loops ────────────────────────────────────────
    _monitor = AccountMonitor(_account_manager, _usage, _pool)
    _monitor.start()

    janitor_task: asyncio.Task | None = None
    if Config.IMAGE_RETENTION_MINUTES > 0:
        janitor_task = asyncio.create_task(
            janitor_loop(
                swept_directories(),
                ttl_seconds=Config.IMAGE_RETENTION_MINUTES * 60,
                interval_seconds=Config.JANITOR_INTERVAL_SECONDS,
            )
        )
    else:
        log.info("IMAGE_RETENTION_MINUTES=0 — downloads are kept forever")

    log.info(
        f"API server ready — provider={provider_name}, "
        f"accounts={len(_account_manager.all())}, tabs={_pool.size()}"
    )

    yield  # Server is running

    await _monitor.stop()
    if janitor_task is not None:
        janitor_task.cancel()
        try:
            await janitor_task
        except asyncio.CancelledError:
            pass
    try:
        await _usage.save()
    except Exception:
        pass
    log.info("Shutting down — closing all accounts...")
    await _account_manager.close_all()
    log.info("All accounts closed")


app = FastAPI(
    title="miri-api",
    description=(
        "Browser automation API for ChatGPT and Claude. "
        "Sends messages via browser and returns responses."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── API key auth middleware ─────────────────────────────────────
class BearerTokenMiddleware:
    """
    Pure ASGI middleware for API-key auth.

    Accepts the key EITHER in the URL path (primary) or in an Authorization
    header (kept working so existing clients don't break):

        POST /<api_key>/v1/images/generations     <- path form
        POST /v1/images/generations
             Authorization: Bearer <api_key>      <- header form

    The path form is handled by stripping the /<api_key> prefix out of the ASGI
    scope before the request reaches the router. Every route stays declared at
    its normal path (/v1/...), so routing, /docs and openapi.json are unaffected.

    Uses raw ASGI protocol instead of BaseHTTPMiddleware to avoid the
    Python 3.9 event-loop mismatch bug that corrupts asyncio.Lock
    when exceptions propagate through BaseHTTPMiddleware's task group.
    """

    # Paths served without a token: interactive docs and the container
    # health-check (which must work before/without any token being set).
    # /admin is the control-panel SHELL (HTML/JS only, no secrets). It is open;
    # every /admin/api/* endpoint under it is key-gated inside manage_routes.
    OPEN_PATHS = {"/docs", "/redoc", "/openapi.json", "/healthz", "/admin"}

    # Generated media (see src/api/media_files.py). Each link carries an HMAC
    # over its file name keyed on the API token, so the URL IS the credential —
    # which is what lets a browser use it directly as <img src> / <video src>.
    # Without this a front end could not display what it just generated without
    # putting the API key in the page URL.
    OPEN_PREFIXES = ("/v1/files/",)

    # Docs pages are refused when reached UNDER a key prefix. Swagger/ReDoc pull
    # JS+CSS from a public CDN, and the browser sends the full URL — key and all
    # — to that third party in the Referer header. They stay available
    # un-prefixed via OPEN_PATHS.
    DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def _deny(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": (
                        "Invalid or missing API key. Put it in the path "
                        "(POST /<api_key>/v1/images/generations) or send "
                        "Authorization: Bearer <api_key>. Note: /docs, /redoc and "
                        "/openapi.json are only served WITHOUT a key prefix, so the "
                        "key is never leaked to the docs CDN via the Referer header."
                    ),
                    "type": "auth_error",
                }
            },
        )
        await response(scope, receive, send)

    @staticmethod
    def _valid_tokens() -> list[str]:
        """Tokens the main gate accepts: API_TOKEN plus ADMIN_TOKEN (both if set).

        Admin routes carry ADMIN_TOKEN, so the middleware must let it through;
        the routes themselves then require the admin-specific token.
        """
        return [t for t in (Config.API_TOKEN, Config.ADMIN_TOKEN) if t]

    @staticmethod
    def _split_key(path: str, tokens: list[str]) -> tuple[bool, str, str]:
        """If `path` starts with /<one-of-tokens>, return (True, matched, remainder).

        Compared with compare_digest so the key cannot be recovered by timing.
        """
        if not path.startswith("/"):
            return False, "", path
        first, slash, rest = path[1:].partition("/")
        if not first:
            return False, "", path
        for tok in tokens:
            if secrets.compare_digest(first, tok):
                return True, tok, ("/" + rest if slash else "/")
        return False, "", path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Only HTTP is served today. Anything else (e.g. a websocket route
            # added later) is denied rather than silently forwarded
            # unauthenticated, which is what this used to do.
            if scope["type"] == "websocket":
                return
            await self.app(scope, receive, send)
            return

        # Empty API_TOKEN keeps the MAIN api open (documented switch). Admin
        # routes stay protected regardless via their own require_admin gate.
        if not Config.API_TOKEN:
            await self.app(scope, receive, send)
            return
        tokens = self._valid_tokens()

        path = scope.get("path", "")

        if path in self.OPEN_PATHS or path.startswith(self.OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return

        # ── Key in the path: /<api_key>/v1/... ──
        matched, _tok, stripped = self._split_key(path, tokens)
        if matched:
            if stripped in self.DOCS_PATHS:
                # Refuse the docs UI under a key prefix. Swagger/ReDoc load JS
                # and CSS from a public CDN, and the browser would send the
                # full URL — key included — to that third party in the Referer
                # header. The docs are already reachable un-prefixed.
                await self._deny(scope, receive, send)
                return

            # Mutate the scope IN PLACE — do not rebind to a copy. uvicorn's
            # access logger keeps a reference to THIS dict and reads
            # scope["path"] when the response starts, i.e. after this returns.
            # Mutating in place means it logs the stripped path and the key is
            # never written to the log at all (RedactKeyFilter is the backstop,
            # not the primary defence).
            scope["path"] = stripped
            # raw_path is the UNDECODED target. Strip its first segment
            # independently: `path` is percent-decoded, so byte offsets do not
            # line up. Rewrite rather than delete, so nothing downstream can
            # recover the key from a stale value.
            raw = scope.get("raw_path")
            if isinstance(raw, (bytes, bytearray)):
                _, rslash, rrest = bytes(raw)[1:].partition(b"/")
                scope["raw_path"] = (rslash + rrest) or b"/"
            await self.app(scope, receive, send)
            return

        # Read the Authorization header off the raw ASGI header list.
        # NOT dict(scope["headers"]): headers are a list of (name, value) pairs
        # that may legitimately repeat, and dict() silently keeps only the last
        # one — so validity depended on header order. Duplicates are rejected.
        auth_headers = [v for (k, v) in scope.get("headers", []) if k == b"authorization"]

        provided = ""
        if len(auth_headers) == 1:
            auth_value = auth_headers[0].decode("latin-1")
            if auth_value.startswith("Bearer "):
                provided = auth_value[7:]

        # compare_digest, not ==: a plain comparison short-circuits on the first
        # differing byte, leaking the token prefix through response timing.
        if not (provided and any(secrets.compare_digest(provided, t) for t in tokens)):
            await self._deny(scope, receive, send)
            return

        await self.app(scope, receive, send)


class RedactKeyFilter(logging.Filter):
    """Strip the API key out of uvicorn's access log lines.

    uvicorn logs every request path verbatim ('POST /<key>/v1/... 200 OK'). With
    the key in the URL that writes the credential to disk on every single
    request, into a bind-mounted log directory. Access logs are also the classic
    place URL-embedded secrets leak from, so redact before it is ever formatted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact BOTH tokens (either can appear in a path).
        for token in (Config.API_TOKEN, Config.ADMIN_TOKEN):
            if not token:
                continue
            if record.args:
                record.args = tuple(
                    a.replace(token, "<key>") if isinstance(a, str) and token in a else a
                    for a in record.args
                )
            if isinstance(record.msg, str) and token in record.msg:
                record.msg = record.msg.replace(token, "<key>")
        return True


# Applied at import so it covers uvicorn started either via __main__ below or
# externally (e.g. `uvicorn src.api.server:app`).
logging.getLogger("uvicorn.access").addFilter(RedactKeyFilter())

# Middleware runs outer->inner in REVERSE add order (last added is outermost).
# Execution: CORS -> BearerToken -> RequestLog -> TemporaryChat -> app.
# RequestLog is INNER to BearerToken on purpose: it then sees the key-STRIPPED
# path and only requests that already passed auth.
from src.providers.temporary_chat import TemporaryChatMiddleware
app.add_middleware(TemporaryChatMiddleware)
app.add_middleware(RequestLogMiddleware, store=_request_log)

app.add_middleware(BearerTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(openai_router)
app.include_router(media_router)
app.include_router(manage_router)

# Give the control-plane routes access to the shared request-log store.
set_request_log(_request_log)


@app.exception_handler(BrowserBusy)
async def browser_busy_handler(request: Request, exc: BrowserBusy) -> JSONResponse:
    """All schedulable tabs are saturated — shed load with 429 instead of hanging."""
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": str(exc),
                "type": "browser_busy",
                "code": "rate_limit_exceeded",
            }
        },
        headers={"Retry-After": "30"},
    )


@app.exception_handler(AccountUnavailable)
async def account_unavailable_handler(request: Request, exc: AccountUnavailable) -> JSONResponse:
    """A chained request whose owning account is down. NEVER rerouted -> 409."""
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "message": str(exc),
                "type": "account_unavailable",
                "code": "conversation_account_unavailable",
                "account_id": exc.account_id,
            }
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Unauthenticated health-check for Docker / load-balancers. Touches no browser."""
    return {
        "status": "ok",
        "workers": _pool.size() if _pool else 0,
        "schedulable_workers": _pool.schedulable_size() if _pool else 0,
        "queue_depth": _pool.queue_depth() if _pool else 0,
        "accounts": len(_account_manager.all()) if _account_manager else 0,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.server:app",
        host=Config.API_HOST,
        port=Config.API_PORT,
        log_level="info",
    )
