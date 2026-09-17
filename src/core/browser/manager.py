"""
Browser lifecycle manager — launch, persist, close.

Uses a persistent Chrome context so the user only signs in once.
Session data (cookies, localStorage, IndexedDB) survives restarts.
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from patchright.async_api import async_playwright, BrowserContext, Page, Playwright

from src.core.config import Config
from src.providers.base import all_domains
from src.core.browser.stealth import apply_stealth
from src.core.log import setup_logging

log = setup_logging("browser")

# The historical hardcoded timezone. Kept as the fallback so no-proxy setups
# behave exactly as before, and used to detect "proxy set, timezone forgotten".
_DEFAULT_TIMEZONE = "America/Los_Angeles"


@dataclass
class AccountLaunchSpec:
    """Everything that makes one account's browser context distinct.

    Each account = one persistent context over its own user_data_dir. Passing
    these explicitly (instead of BrowserManager reading Config globals) is what
    lets one process host M independent accounts without their settings bleeding
    into each other.
    """
    account_id: str
    label: str
    user_data_dir: Path
    proxy: dict | None       # already-built Playwright proxy dict, or None
    timezone_id: str         # resolved IANA name — never "auto" at this point
    locale: str
    provider: str = "chatgpt"  # which service this context signs into

    @classmethod
    async def from_config(
        cls,
        *,
        account_id: str = "default",
        label: str = "default",
        user_data_dir: Path | None = None,
        proxy: dict | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        provider: str = "chatgpt",
    ) -> "AccountLaunchSpec":
        """Build a spec, resolving the global-config defaults + auto timezone.

        This preserves the exact single-account behavior: with no arguments it
        reproduces what start() used to read straight from Config.
        """
        proxy = proxy if proxy is not None else Config.proxy_config()
        tz = (timezone or Config.BROWSER_TIMEZONE)
        if tz.lower() == "auto":
            detected = await asyncio.to_thread(_detect_timezone_via_proxy, proxy)
            if detected:
                tz = detected
            else:
                tz = _DEFAULT_TIMEZONE
                log.warning(
                    f"BROWSER_TIMEZONE=auto failed for {account_id} — falling back "
                    f"to {tz}. Set BROWSER_TIMEZONE (or the account's timezone) if "
                    "the proxy exits elsewhere."
                )
        elif proxy is not None and tz == _DEFAULT_TIMEZONE:
            log.warning(
                f"Account {account_id}: a proxy is configured but timezone is still "
                f"the default {_DEFAULT_TIMEZONE}. If the proxy does not exit in "
                "US/Pacific this is a fingerprint mismatch — set the timezone."
            )
        return cls(
            account_id=account_id,
            label=label,
            user_data_dir=user_data_dir or Config.BROWSER_DATA_DIR,
            proxy=proxy,
            timezone_id=tz,
            locale=locale or Config.BROWSER_LOCALE,
            provider=provider,
        )


def _resolve_domains_for_chrome() -> str:
    """
    Pre-resolve key domains via the OS and return a --host-resolver-rules
    string for Chrome.

    Chrome's built-in DNS client (even with --disable-features=AsyncDns)
    is unreliable — it can return DNS_PROBE_FINISHED_NXDOMAIN for domains
    that the OS resolver handles fine.  By pre-resolving here and passing
    the IPs via --host-resolver-rules, Chrome bypasses its own resolver
    entirely and the problem disappears.

    Returns empty string if all resolutions fail.
    """
    rules = []
    for domain in all_domains():
        try:
            ip = socket.gethostbyname(domain)
            rules.append(f"MAP {domain} {ip}")
            log.debug(f"DNS pre-resolve: {domain} -> {ip}")
        except Exception as e:
            log.warning(f"DNS pre-resolve failed: {domain} -> {e}")

    if rules:
        result = ", ".join(rules)
        log.info(f"Chrome host-resolver-rules: {len(rules)} domains mapped")
        return result
    return ""


def _detect_timezone_via_proxy(proxy: dict | None, timeout: int = 10) -> str | None:
    """Return the IANA timezone of the exit IP, or None if it can't be determined.

    The lookup is deliberately routed through the SAME proxy the browser will
    use — querying direct would report the container's own region, which is
    exactly the mismatch this exists to prevent.

    Blocking (urllib); call via asyncio.to_thread.
    """
    import json
    import urllib.request

    if proxy is not None:
        server = proxy["server"]
        if server.startswith("socks"):
            # urllib cannot speak SOCKS without PySocks. Rather than silently
            # querying direct (which would report the wrong region), decline.
            log.warning(
                "BROWSER_TIMEZONE=auto cannot detect through a SOCKS proxy — "
                "set BROWSER_TIMEZONE to an explicit IANA name (e.g. Europe/Berlin)"
            )
            return None
        # Rebuild the URL with credentials, which is how urllib takes proxy auth.
        if proxy.get("username"):
            scheme, rest = server.split("://", 1)
            user = quote(proxy["username"], safe="")
            pw = quote(proxy.get("password", ""), safe="")
            server = f"{scheme}://{user}:{pw}@{rest}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": server, "https": server})
        )
    else:
        opener = urllib.request.build_opener()

    # Endpoints that return a timezone for the caller's IP, tried in order.
    endpoints = [
        ("http://ip-api.com/json/?fields=status,timezone,countryCode,query", "timezone"),
        ("https://ipinfo.io/json", "timezone"),
    ]
    for url, key in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "miri-api"})
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read(64 * 1024).decode("utf-8", "replace"))
            tz = data.get(key)
            if tz:
                log.info(
                    f"Geo-detected exit IP {data.get('query') or data.get('ip') or '?'} "
                    f"-> timezone {tz}"
                )
                return tz
        except Exception as e:
            log.debug(f"Timezone lookup via {url} failed: {e}")
    log.warning("Could not auto-detect timezone from the exit IP")
    return None


def _cleanup_stale_locks(data_dir: Path) -> None:
    """
    Remove stale lock / journal / WAL files that prevent browser launch.

    After a crash, Chromium leaves behind:
    - SingletonLock/Socket/Cookie — prevents new instance from using data dir.
    - *-journal, *-wal, *-shm — SQLite journal/WAL files that cause
      "database is locked" errors (UKM, Top Sites, History, etc.)

    We also attempt to kill any orphan Chromium processes that are using
    our user-data-dir.
    """
    import subprocess

    # 1. Kill orphan Chromium processes holding THIS profile.
    #
    #    BOUNDARY-ANCHORED. A bare "--user-data-dir=/app/browser_data" is a
    #    PREFIX of every sub-account dir "/app/browser_data/_accounts/<id>", so
    #    the default account's cleanup would pkill EVERY other account's live
    #    browser. The trailing ([[:space:]]|$) requires the path to end exactly
    #    here (Chrome's cmdline has more flags after it, or ends), so
    #    /app/browser_data matches only the default and never a nested account,
    #    and /app/browser_data/_accounts/acct1 never matches acct10.
    pattern = f"--user-data-dir={data_dir}([[:space:]]|$)"
    try:
        result = subprocess.run(
            ["pkill", "-9", "-f", pattern],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            log.info(f"Killed orphan browser process holding {data_dir}")
            import time
            time.sleep(1)
    except Exception:
        pass  # Non-critical — the lock-file cleanup below is the real fallback

    # 2. Remove singleton lock files
    lock_files = ["SingletonLock", "SingletonSocket", "SingletonCookie"]
    for name in lock_files:
        path = data_dir / name
        if path.exists():
            try:
                path.unlink()
                log.info(f"Removed stale lock file: {name}")
            except Exception as e:
                log.warning(f"Could not remove {name}: {e}")

    # 3. Remove SQLite journal/WAL/SHM files that cause "database is locked".
    #    MUST NOT descend into the _accounts/ subtree: when the default account
    #    owns the volume root, "**/*-wal" would match OTHER accounts' live WAL
    #    files under _accounts/<id>/ and corrupt their running profiles. Skip any
    #    match whose path crosses into _accounts.
    import glob as _glob
    patterns = ["**/*-journal", "**/*-wal", "**/*-shm"]
    removed = 0
    for pattern in patterns:
        for path_str in _glob.glob(str(data_dir / pattern), recursive=True):
            try:
                rel = Path(path_str).relative_to(data_dir)
            except ValueError:
                continue
            if "_accounts" in rel.parts:
                continue  # belongs to a different account — never touch it
            try:
                Path(path_str).unlink()
                removed += 1
            except Exception:
                pass
    if removed:
        log.info(f"Removed {removed} stale SQLite journal/WAL/SHM files")

    # 4. Clear ALL network / DNS / cache state that can corrupt Chrome's
    #    resolver and cause DNS_PROBE_FINISHED_NXDOMAIN for every domain.
    #    Chrome's built-in DNS client stores state in the persistent profile
    #    that survives restarts and can poison resolution for ALL sites.
    import shutil

    # 4a. Delete network state files (DNS, QUIC, HTTP/3 connection cache)
    network_files = [
        "Default/Network Persistent State",
        "Default/Network Action Predictor",
        "Default/TransportSecurity",
        "Default/Reporting and NEL",
        "Default/SCT Auditing Pending Reports",
        "Default/ServerCertificate",
        "Default/DIPS",
        "Default/Safe Browsing Cookies",
    ]
    for rel_path in network_files:
        fpath = data_dir / rel_path
        if fpath.exists():
            try:
                fpath.unlink()
                log.info(f"Cleared network state: {rel_path}")
            except Exception:
                pass

    # 4b. Delete cache directories (HTTP cache, compiled JS, GPU shaders).
    #     These can grow large and contain stale connection/DNS info.
    cache_dirs = [
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/DawnGraphiteCache",
        "Default/DawnWebGPUCache",
        "Default/Service Worker",
        "GrShaderCache",
        "GraphiteDawnCache",
        "ShaderCache",
    ]
    for rel_dir in cache_dirs:
        dpath = data_dir / rel_dir
        if dpath.exists() and dpath.is_dir():
            try:
                shutil.rmtree(dpath, ignore_errors=True)
                log.info(f"Cleared cache directory: {rel_dir}")
            except Exception:
                pass


class BrowserManager:
    """Manages one persistent Chromium browser context (one account).

    spec: the account's launch parameters. If None, built from Config on start()
    (the single-account default path — byte-for-byte the old behavior).
    playwright: an injected driver. If None, this manager starts and owns its own
    (single-account path). Multi-account shares ONE driver across managers, so
    AccountManager injects it and this manager must NOT stop it on close().
    """

    def __init__(
        self,
        spec: "AccountLaunchSpec | None" = None,
        playwright: Playwright | None = None,
    ) -> None:
        self._spec = spec
        self._playwright = playwright
        self._owns_playwright = playwright is None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def account_id(self) -> str:
        return self._spec.account_id if self._spec else "default"

    def provider_url(self) -> str:
        """Where THIS context's tabs live (per-provider, not a global)."""
        from src.providers.base import get_provider
        return get_provider(self._spec.provider if self._spec else "chatgpt").url

    async def start(self) -> Page:
        """
        Launch a persistent Chrome context with stealth and human-like settings.

        Automatically cleans up stale lock files from previous crashed sessions.
        Returns the active page ready for navigation.
        """
        Config.ensure_dirs()

        if self._spec is None:
            self._spec = await AccountLaunchSpec.from_config()
        spec = self._spec
        spec.user_data_dir.mkdir(parents=True, exist_ok=True)

        # Clean up stale locks from previous sessions — scoped to THIS profile.
        _cleanup_stale_locks(spec.user_data_dir)

        log.info(f"Launching browser for account '{spec.account_id}'...")
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        # Randomize viewport slightly to avoid fingerprint consistency
        width = Config.VIEWPORT_WIDTH + random.randint(-20, 20)
        height = Config.VIEWPORT_HEIGHT + random.randint(-20, 20)

        # Try real Chrome first, fall back to bundled Chromium
        chrome_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            # Disable Chrome's built-in DNS client entirely.  Even with
            # AsyncDns off, Chrome's stub resolver can return NXDOMAIN for
            # domains the OS resolves fine.  We also pre-resolve domains
            # via --host-resolver-rules (see _resolve_domains_for_chrome).
            "--disable-features=AsyncDns,DnsOverHttps",
            "--dns-prefetch-disable",
        ]

        # Docker-specific flags
        if os.path.exists("/.dockerenv") or os.environ.get("DISPLAY") == ":99":
            chrome_args.extend([
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ])

        # Proxy + timezone come from the spec (already resolved, per account).
        proxy = spec.proxy

        if proxy is None:
            # Pre-resolve domains via the OS and hardcode the IPs for Chrome.
            # This prevents Chrome's built-in DNS client from ever being used.
            resolver_rules = _resolve_domains_for_chrome()
            if resolver_rules:
                chrome_args.append(f"--host-resolver-rules={resolver_rules}")
        else:
            # With a proxy, the PROXY resolves the destination — Chrome sends
            # `CONNECT chatgpt.com:443` and never looks it up itself. Pinning
            # locally-resolved IPs here would be dead weight at best, and at
            # worst actively wrong: CDN IPs resolved from the container's own
            # egress while traffic actually exits at the proxy's region.
            log.info(f"Account '{spec.account_id}': proxy enabled ({proxy.get('server')})")
            log.info("Skipping local DNS pre-resolution — the proxy resolves destinations")

        timezone_id = spec.timezone_id
        log.info(
            f"Account '{spec.account_id}' identity: timezone={timezone_id} "
            f"locale={spec.locale}"
        )

        launch_kwargs = dict(
            user_data_dir=str(spec.user_data_dir),
            headless=Config.HEADLESS,
            slow_mo=Config.SLOW_MO,
            viewport={"width": width, "height": height},
            locale=spec.locale,
            timezone_id=timezone_id,
            args=chrome_args,
        )

        # Both launch paths below splat this same dict, so setting the key here
        # covers channel="chrome" AND the bundled-Chromium fallback. The key is
        # added only when a proxy exists, so the no-proxy path stays byte-for-byte
        # identical to previous behavior.
        # NOTE: pass the proxy DICT — do not hand-append --proxy-server to
        # chrome_args. Playwright derives --proxy-server/--proxy-bypass-list from
        # this dict; passing both would be a conflicting duplicate switch and
        # would bypass Playwright's credential-challenge handler.
        if proxy is not None:
            launch_kwargs["proxy"] = proxy

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            log.info("Launched with real Chrome")
        except Exception as e:
            # Do not assert a cause we never checked: this path also catches
            # proxy rejections, and in Docker it is the guaranteed path since
            # only bundled Chromium is installed.
            log.info(f"Real Chrome launch failed ({type(e).__name__}: {e}) — using bundled Chromium")
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )

        # NOTE: Stealth patches are applied AFTER the first navigation.
        # In Docker, applying stealth init scripts before navigation
        # causes Chrome's DNS resolver to fail (ERR_NAME_NOT_RESOLVED).
        # Call apply_stealth_patches() after navigating to the target page.

        # Use existing page or create one
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = await self._context.new_page()

        # NOTE: We intentionally do NOT flush Chrome's DNS cache here.
        # The --host-resolver-rules flag handles DNS resolution for all
        # mapped domains.  Previously, _clear_dns_cache() would navigate
        # to chrome://net-internals and flush the host cache + socket
        # pools — but this destroyed working connection state and caused
        # DNS_PROBE_FINISHED_NXDOMAIN on subsequent navigations.

        log.info(f"Browser ready — viewport {width}x{height}")
        return self._page

    async def _clear_dns_cache(self) -> None:
        """Clear Chrome's in-memory DNS host cache via chrome://net-internals."""
        import asyncio as _asyncio

        if self._page is None:
            return

        try:
            await self._page.goto(
                "chrome://net-internals/#dns",
                wait_until="domcontentloaded",
                timeout=10000,
            )
            await _asyncio.sleep(0.5)

            # The "Clear host cache" button ID in chrome://net-internals/#dns
            cleared = await self._page.evaluate(
                """
                () => {
                    // Try the standard button
                    const btn = document.getElementById('dns-view-clear-cache');
                    if (btn) { btn.click(); return 'clicked-dns-view-clear-cache'; }
                    // Newer Chrome: look for any button that says "Clear"
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const b of buttons) {
                        if (b.textContent.toLowerCase().includes('clear')) {
                            b.click();
                            return 'clicked-' + b.textContent.trim();
                        }
                    }
                    return 'no-clear-button-found';
                }
                """
            )
            log.info(f"Chrome DNS cache flush: {cleared}")
            await _asyncio.sleep(0.3)

            # Also try to flush socket pools
            try:
                await self._page.goto(
                    "chrome://net-internals/#sockets",
                    wait_until="domcontentloaded",
                    timeout=5000,
                )
                await _asyncio.sleep(0.3)
                await self._page.evaluate(
                    """
                    () => {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        for (const b of buttons) {
                            if (b.textContent.toLowerCase().includes('flush') ||
                                b.textContent.toLowerCase().includes('close')) {
                                b.click();
                            }
                        }
                    }
                    """
                )
                log.info("Chrome socket pools flushed")
            except Exception:
                pass  # Best-effort

        except Exception as e:
            log.warning(f"Could not clear Chrome DNS cache: {e}")

    async def apply_stealth_patches(self) -> None:
        """
        Apply stealth patches to the browser context.

        Must be called AFTER the first page navigation, not before.
        In Docker containers, applying stealth init scripts before any
        navigation causes Chrome's DNS resolver to fail.
        """
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        await apply_stealth(self._context)

    @property
    def page(self) -> Page:
        """Get the active page. Raises if browser not started."""
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    @property
    def context(self) -> BrowserContext:
        """Get the browser context."""
        if self._context is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._context

    async def navigate(self, url: str) -> None:
        """Navigate to a URL and wait for page load."""
        log.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until="domcontentloaded")
        log.info("Page loaded")

    async def new_worker_page(self, index: int) -> Page:
        """Open an additional tab and navigate it to the provider.

        Used to build the concurrency pool: each extra worker gets its own tab
        in this same context, so all tabs share the one login. Stealth is a
        context-level init script (see apply_stealth), so it already covers new
        pages — no per-page stealth call needed.
        """
        if self._context is None:
            raise RuntimeError("Browser not started")
        target = self.provider_url()
        page = await self._context.new_page()
        log.info(f"Worker {index}: opening tab -> {target}")
        # Retry navigation the same way the server startup does (Docker DNS can
        # be slow right after launch).
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                last_err = None
                break
            except Exception as e:
                last_err = e
                log.warning(f"Worker {index}: nav attempt {attempt}/3 failed: {e}")
                await asyncio.sleep(attempt * 3)
        if last_err is not None:
            log.error(f"Worker {index}: navigation failed: {last_err}")
            raise last_err
        return page

    async def recover_page(self, page: Page | None = None) -> bool:
        """Recover from DNS / page errors by re-navigating to ChatGPT.

        Tries JS navigation first (avoids DNS lookup), then page.goto().
        Returns True if recovery succeeded, False otherwise.

        `page` defaults to the primary page; with a worker pool each worker
        passes its own page so recovery acts on the right tab.
        """
        import asyncio as _asyncio

        page = page or self._page
        if page is None:
            return False

        # Strategy 1: JS navigation (doesn't go through Chrome's DNS resolver)
        try:
            log.info("Page recovery via JS navigation...")
            # provider_url(), not CHATGPT_URL: recovering a Claude session by
            # navigating to ChatGPT would hijack the browser to the wrong site.
            await page.evaluate(f"window.location.href = '{self.provider_url()}'")
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await _asyncio.sleep(1)
            error = await page.evaluate(
                """
                () => {
                    const body = document.body ? document.body.innerText : '';
                    if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'dns';
                    if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'dns';
                    if (body.includes('ERR_CONNECTION_REFUSED')) return 'conn';
                    // A dead/misconfigured proxy is not recoverable by retrying.
                    if (body.includes('ERR_PROXY_CONNECTION_FAILED')) return 'proxy';
                    if (body.includes('ERR_TUNNEL_CONNECTION_FAILED')) return 'proxy';
                    return null;
                }
                """
            )
            if not error:
                log.info("Page recovery succeeded (JS navigation)")
                return True
            log.warning(f"JS navigation recovery still shows error: {error}")
        except Exception as e:
            log.warning(f"JS navigation recovery failed: {e}")

        # Strategy 2: page.goto() with retries
        for attempt in range(1, 4):
            try:
                log.info(f"Page recovery attempt {attempt}/3 (page.goto)...")
                await page.goto(
                    self.provider_url(),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await _asyncio.sleep(1)

                error = await page.evaluate(
                    """
                    () => {
                        const body = document.body ? document.body.innerText : '';
                        if (body.includes('DNS_PROBE_FINISHED_NXDOMAIN')) return 'dns';
                        if (body.includes('ERR_NAME_NOT_RESOLVED')) return 'dns';
                        if (body.includes('ERR_CONNECTION_REFUSED')) return 'conn';
                    // A dead/misconfigured proxy is not recoverable by retrying.
                    if (body.includes('ERR_PROXY_CONNECTION_FAILED')) return 'proxy';
                    if (body.includes('ERR_TUNNEL_CONNECTION_FAILED')) return 'proxy';
                        return null;
                    }
                    """
                )
                if error:
                    log.warning(f"Recovery attempt {attempt} still shows error: {error}")
                    await _asyncio.sleep(attempt * 2)
                    continue

                log.info("Page recovery succeeded")
                return True

            except Exception as e:
                log.warning(f"Recovery attempt {attempt} failed: {e}")
                await _asyncio.sleep(attempt * 2)

        log.error("Page recovery failed after all attempts")
        return False

    async def is_logged_in(self, page: Page | None = None, *, timeout_ms: int = 8000) -> bool:
        """Recognize a restored session after hydration, including another login tab."""
        from src.providers.base import get_provider
        from src.core.browser.login import wait_for_login

        spec = get_provider(self._spec.provider if self._spec else "chatgpt")

        def pages():
            if page is not None:
                return [page]
            # Only this account's context; the helper filters foreign origins.
            return list(self._context.pages) if self._context else []

        try:
            ok = await asyncio.wait_for(wait_for_login(pages, spec, timeout_ms=timeout_ms),
                                        timeout=max(0, timeout_ms) / 1000 + 1)
            log.debug(f"[{self.account_id}] {spec.label} login check: {ok}")
            return ok
        except Exception as e:
            log.debug(f"[{self.account_id}] login check not ready: {e}")
            return False

    async def close(self) -> None:
        """Close this account's context.

        Only stops the Playwright driver if THIS manager owns it (single-account
        path). When the driver was injected (multi-account, shared across M
        contexts), stopping it here would kill every other account — so we close
        only our own context and leave the driver to its owner (AccountManager).
        """
        log.info(f"Closing browser for account '{self.account_id}'...")
        try:
            if self._context:
                await self._context.close()
            if self._playwright and self._owns_playwright:
                await self._playwright.stop()
        except Exception as e:
            log.error(f"Error closing browser: {e}")
        finally:
            self._context = None
            self._page = None
            if self._owns_playwright:
                self._playwright = None
            log.info(f"Browser closed for account '{self.account_id}'")
