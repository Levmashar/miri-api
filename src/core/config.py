"""
Centralized configuration — loads from .env with sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

_CODE_ROOT = Path(__file__).resolve().parent.parent
_CWD = Path.cwd()

# Prefer the invocation directory as project root when running from
# a checkout (e.g. `nix run .#proxy` from repo root). Fall back to the
# code location (used for packaged/store execution).
if (_CWD / "src").exists() and (_CWD / "scripts").exists():
    _PROJECT_ROOT = _CWD
else:
    _PROJECT_ROOT = _CODE_ROOT

# Load .env from current working directory first, then from the
# resolved project root.
load_dotenv(_CWD / ".env")
# Skip the second parse when the checkout root IS the CWD (the normal case, and
# what the container does at WORKDIR /app) — otherwise .env is read twice.
if _PROJECT_ROOT != _CWD:
    load_dotenv(_PROJECT_ROOT / ".env")


class Config:
    """All project settings in one place."""

    TEMPORARY_CHATS: bool = os.getenv("TEMPORARY_CHATS", "0").strip() == "1"
    # Start every generation request in a fresh saved chat.  The admin panel can
    # change this at runtime; the environment value is the initial default.
    NEW_CHAT_EVERY_REQUEST: bool = os.getenv("NEW_CHAT_EVERY_REQUEST", "0").strip() == "1"

    # Paths
    PROJECT_ROOT: Path = _PROJECT_ROOT
    BROWSER_DATA_DIR: Path = _PROJECT_ROOT / os.getenv("BROWSER_DATA_DIR", "browser_data")
    LOG_DIR: Path = _PROJECT_ROOT / os.getenv("LOG_DIR", "logs")
    IMAGES_DIR: Path = _PROJECT_ROOT / os.getenv("IMAGES_DIR", "downloads/images")
    VIDEOS_DIR: Path = _PROJECT_ROOT / os.getenv("VIDEOS_DIR", "downloads/videos")

    # Browser
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    SLOW_MO: int = int(os.getenv("SLOW_MO", "25"))
    CHATGPT_URL: str = os.getenv("CHATGPT_URL", "https://chatgpt.com")
    CLAUDE_URL: str = os.getenv("CLAUDE_URL", "https://claude.ai")

    # Provider selection: "chatgpt" or "claude"
    # Legacy single-provider switch. Providers now run SIMULTANEOUSLY — the
    # provider is a property of each ACCOUNT — so this only decides which one
    # serves a request that names no provider (no path prefix, unknown model).
    PROVIDER: str = os.getenv("PROVIDER", "chatgpt").lower()
    DEFAULT_PROVIDER: str = os.getenv("DEFAULT_PROVIDER", os.getenv("PROVIDER", "chatgpt")).lower()

    # Gemini image generation: the app generates with Nano Banana 2, then offers
    # "Redo with Nano Banana Pro" on PAID Google AI plans only. When true we take
    # that second step; on a free account the entry is absent and we keep the
    # Nano Banana 2 image rather than failing.
    GEMINI_USE_NANO_BANANA_PRO: bool = os.getenv(
        "GEMINI_USE_NANO_BANANA_PRO", "true").lower() == "true"

    # Seedream / Dreamina (video). The default video model used when a request
    # names none. Callers pick a specific model per request (see the video model
    # list in src/providers/base.py).
    SEEDREAM_DEFAULT_MODEL: str = os.getenv("SEEDREAM_DEFAULT_MODEL", "").strip()

    @classmethod
    def provider_url(cls) -> str:
        """Return the target URL for the active provider."""
        if cls.PROVIDER == "claude":
            return cls.CLAUDE_URL
        return cls.CHATGPT_URL

    # Timeouts (ms)
    RESPONSE_TIMEOUT: int = int(os.getenv("RESPONSE_TIMEOUT", "120000"))
    SELECTOR_TIMEOUT: int = int(os.getenv("SELECTOR_TIMEOUT", "10000"))
    # Video generation is far slower than chat/image (a Dreamina clip can take
    # minutes to render), so it gets its own, much longer completion deadline.
    IMAGE_RESPONSE_TIMEOUT: int = int(os.getenv("IMAGE_RESPONSE_TIMEOUT", "300000"))
    VIDEO_RESPONSE_TIMEOUT: int = int(os.getenv("VIDEO_RESPONSE_TIMEOUT", "600000"))
    # Slow CHAT models/modes picked in the provider UI — Gemini Deep Research /
    # Deep Think, Grok DeepSearch / Heavy — work for minutes before writing the
    # answer, so the normal chat deadline would expire mid-run. Only requests
    # that ask for one of those models use this (see the `long` flag in
    # src/providers/{gemini,grok}/models.py).
    LONG_MODE_TIMEOUT: int = int(os.getenv("LONG_MODE_TIMEOUT", "900000"))

    # Human simulation (ms)
    TYPING_SPEED_MIN: int = int(os.getenv("TYPING_SPEED_MIN", "50"))
    TYPING_SPEED_MAX: int = int(os.getenv("TYPING_SPEED_MAX", "150"))
    THINKING_PAUSE_MIN: int = int(os.getenv("THINKING_PAUSE_MIN", "500"))
    THINKING_PAUSE_MAX: int = int(os.getenv("THINKING_PAUSE_MAX", "1500"))
    # Completion poll interval — how often to check if response is ready (ms)
    POLL_INTERVAL_MS: int = int(os.getenv("POLL_INTERVAL_MS", "300"))

    # ── Downloads retention ───────────────────────────────────────
    # How long a generated image / downloaded attachment stays on disk after
    # it is written. Generated images are multi-MB and were never cleaned up,
    # so the downloads directory grew unbounded.
    # With response_format="url", this is how long that path stays valid.
    # 0 disables cleanup entirely (keep files forever).
    IMAGE_RETENTION_MINUTES: float = float(os.getenv("IMAGE_RETENTION_MINUTES", "10"))
    # How often the janitor sweeps.
    JANITOR_INTERVAL_SECONDS: float = float(os.getenv("JANITOR_INTERVAL_SECONDS", "60"))

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
    VERBOSE: bool = os.getenv("VERBOSE", "true").lower() == "true"
    # Per-logger files rotate daily; keep this many days of rotated files, then
    # they are auto-deleted (bounds the logs dir, which nothing else prunes).
    LOG_FILE_RETENTION_DAYS: int = int(os.getenv("LOG_FILE_RETENTION_DAYS", "14"))

    # ── Concurrency ───────────────────────────────────────────────
    # Tabs per account. Each opens its own tab in that account's browser
    # (shared login within the account). 1 = fully serialized per account.
    MAX_CONCURRENT_REQUESTS: int = max(1, int(os.getenv("MAX_CONCURRENT_REQUESTS", "1")))

    # ── Multi-account ─────────────────────────────────────────────
    # Accounts are managed at runtime via the /admin UI and persisted in
    # accounts.json under BROWSER_DATA_DIR. These are only defaults/policy.
    #
    # Soft request cap per account per window: rotate to the next account BEFORE
    # hitting the hard limit. 0 = disabled (only react to real limit messages).
    # This is an OBSERVED-USAGE estimate, never a real remaining-quota number.
    ACCOUNT_SOFT_CAP: int = int(os.getenv("ACCOUNT_SOFT_CAP", "0"))
    ACCOUNT_SOFT_CAP_WINDOW_MIN: float = float(os.getenv("ACCOUNT_SOFT_CAP_WINDOW_MIN", "180"))
    # Fraction of the soft cap at which an account is flagged "nearing".
    ACCOUNT_NEARING_FRACTION: float = float(os.getenv("ACCOUNT_NEARING_FRACTION", "0.8"))
    # Default cooldown when a limit is hit but no reset time could be parsed (min).
    ACCOUNT_DEFAULT_COOLDOWN_MIN: float = float(os.getenv("ACCOUNT_DEFAULT_COOLDOWN_MIN", "60"))

    # Overflow activation hysteresis (seconds). Promote the next account after
    # the active tier has been saturated (all tabs busy AND requests queued) for
    # EXPAND_HOLD; drain a promoted account after it has been idle for SHRINK_HOLD.
    OVERFLOW_EXPAND_HOLD_S: float = float(os.getenv("OVERFLOW_EXPAND_HOLD_S", "8"))
    OVERFLOW_SHRINK_HOLD_S: float = float(os.getenv("OVERFLOW_SHRINK_HOLD_S", "120"))

    # Admin/control-plane token. Falls back to API_TOKEN if empty. The control
    # plane hard-refuses (403) if BOTH are empty — it can delete accounts.
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

    # ── Request log (admin Logs viewer) ───────────────────────────
    # Persisted in LOG_DIR/requests.sqlite3 and cached in memory, pruned by AGE
    # so the viewer shows the last week. Base64 media is stripped from bodies, so
    # entries stay small; MAX_ENTRIES is only a memory backstop against a runaway
    # burst, deliberately set far above a normal week's traffic.
    LOG_RETENTION_DAYS: float = float(os.getenv("LOG_RETENTION_DAYS", "7"))
    LOG_MAX_ENTRIES: int = int(os.getenv("LOG_MAX_ENTRIES", "50000"))
    # Per-body character cap for what is STORED (after media stripping).
    LOG_BODY_CAP: int = int(os.getenv("LOG_BODY_CAP", str(256 * 1024)))

    # URL the /admin page's embedded VNC iframe points at.
    VNC_PUBLIC_URL: str = os.getenv("VNC_PUBLIC_URL", "")

    # API (Phase 3)
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    RATE_LIMIT_SECONDS: int = int(os.getenv("RATE_LIMIT_SECONDS", "5"))
    API_TOKEN: str = os.getenv("API_TOKEN", "")  # Bearer token for API auth (empty = no auth)

    # Origin used to build the URLs handed back for generated media
    # (response_format="url"). Empty = derive it from each request, honouring
    # X-Forwarded-Proto/Host. Set it when this gateway sits behind a reverse
    # proxy or tunnel whose public address it cannot see.
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").strip()

    # VNC
    VNC_PASSWORD: str = os.getenv("VNC_PASSWORD", "chatgpt")

    # ── Proxy (optional) ──────────────────────────────────────────
    # Raw values only. Parsing/validation lives in proxy_config()
    # because this class body runs at import time — raising here would
    # break every importer of Config with a traceback pointing at the
    # import instead of at the misconfiguration.
    # Empty PROXY_SERVER = direct connection (the default).
    #
    # Deliberately NOT named HTTP_PROXY/HTTPS_PROXY: those are honoured
    # by curl, and the container healthcheck curls localhost — routing
    # that through an external proxy would fail the healthcheck and
    # flap the container under `restart: unless-stopped`.
    PROXY_SERVER: str = os.getenv("PROXY_SERVER", "").strip()
    PROXY_USERNAME: str = os.getenv("PROXY_USERNAME", "").strip()
    # Not stripped — leading/trailing spaces can be significant in a password.
    PROXY_PASSWORD: str = os.getenv("PROXY_PASSWORD", "")
    PROXY_BYPASS: str = os.getenv("PROXY_BYPASS", "localhost,127.0.0.1,::1").strip()

    # Schemes Chromium can actually use for --proxy-server.
    PROXY_SCHEMES: tuple = ("http", "https", "socks5", "socks4")

    # ── Browser identity (fingerprint) ────────────────────────────
    # These MUST agree with the region your traffic actually exits from.
    # Pages read the timezone directly in JS (Date.getTimezoneOffset(),
    # Intl.DateTimeFormat().resolvedOptions().timeZone) and compare it to the
    # IP's region — a German exit IP reporting America/Los_Angeles is a strong
    # bot signal.
    #
    # BROWSER_TIMEZONE accepts:
    #   an IANA name  — e.g. Europe/Berlin  (explicit, predictable)
    #   "auto"        — detect from the proxy's exit IP at launch
    # Default keeps the previous hardcoded value, so nothing changes for
    # direct (no-proxy) setups.
    BROWSER_TIMEZONE: str = os.getenv("BROWSER_TIMEZONE", "America/Los_Angeles").strip()

    # Accept-Language / Intl locale. Left manual: an English-speaking user on a
    # German exit node is entirely plausible, so locale is a much weaker signal
    # than timezone and auto-deriving it from the country would often be wrong.
    BROWSER_LOCALE: str = os.getenv("BROWSER_LOCALE", "en-US").strip()

    # Viewport base (will be jittered ±20px)
    VIEWPORT_WIDTH: int = 1280
    VIEWPORT_HEIGHT: int = 720

    @classmethod
    def proxy_config(cls) -> Optional[dict]:
        """The GLOBAL proxy (from PROXY_SERVER env), or None. See build_proxy()."""
        return cls.build_proxy(
            cls.PROXY_SERVER, cls.PROXY_USERNAME, cls.PROXY_PASSWORD, cls.PROXY_BYPASS
        )

    @classmethod
    def build_proxy(
        cls,
        server: str,
        username: str = "",
        password: str = "",
        bypass: str = "localhost,127.0.0.1,::1",
    ) -> Optional[dict]:
        """
        Build the patchright/Playwright proxy dict, or None when `server` is empty.

        Shape: {"server": "scheme://host:port", "username", "password", "bypass"}

        Reused per-account for multi-account setups (each account may have its
        own proxy). Raises ValueError on anything malformed — a typo'd proxy must
        fail the browser launch loudly rather than silently connecting direct and
        leaking the real IP.
        """
        raw = (server or "").strip()
        if not raw:
            return None  # No proxy configured — connect directly.

        if "://" not in raw:
            raise ValueError(
                f"proxy server must include a scheme "
                f"({'|'.join(cls.PROXY_SCHEMES)}://), got: {raw!r}"
            )

        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()

        if scheme not in cls.PROXY_SCHEMES:
            raise ValueError(
                f"proxy scheme {scheme!r} is not supported by Chromium. "
                f"Use one of: {', '.join(cls.PROXY_SCHEMES)}"
            )
        if not parsed.hostname:
            raise ValueError(f"proxy has no host: {raw!r}")

        # urlparse only validates the port when .port is accessed.
        try:
            port = parsed.port
        except ValueError as e:
            raise ValueError(f"proxy has an invalid port: {raw!r} ({e})") from e
        if port is None:
            raise ValueError(
                f"proxy must include an explicit port "
                f"(e.g. {scheme}://{parsed.hostname}:8080), got: {raw!r}"
            )
        if parsed.path.rstrip("/") or parsed.query or parsed.fragment:
            raise ValueError(
                f"proxy must be scheme://host:port with no path/query, got: {raw!r}"
            )

        # Credentials may be embedded (http://user:pass@host:port) since that is
        # how most providers hand them out. Chromium's --proxy-server IGNORES
        # embedded credentials, so lift them into username/password where
        # Playwright answers the auth challenge. Embedded creds win.
        username = parsed.username or username
        password = parsed.password or password

        # server must be credential-free.
        server = f"{scheme}://{parsed.hostname}:{port}"

        if username and scheme.startswith("socks"):
            # Chromium implements SOCKS with no auth method (crbug.com/256785).
            raise ValueError(
                f"Chromium does not support proxy authentication over {scheme}. "
                "Use an http/https proxy, or an IP-whitelisted SOCKS endpoint "
                "with no username/password."
            )
        if password and not username:
            raise ValueError("proxy password is set but username is empty")

        proxy: dict = {"server": server}
        if username:
            proxy["username"] = username
            proxy["password"] = password
        if bypass:
            proxy["bypass"] = bypass
        return proxy

    @classmethod
    def proxy_log_str(cls) -> str:
        """Redacted one-liner for logs. Never emits the password."""
        proxy = cls.proxy_config()
        if proxy is None:
            return "none (direct)"
        auth = f" auth={proxy['username']}:***" if proxy.get("username") else " auth=none"
        bypass = f" bypass={proxy['bypass']}" if proxy.get("bypass") else ""
        return f"{proxy['server']}{auth}{bypass}"

    @classmethod
    def ensure_dirs(cls) -> None:
        """Create required directories if they don't exist."""
        cls.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        cls.VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
