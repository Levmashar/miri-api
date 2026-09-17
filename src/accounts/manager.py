"""
AccountManager — owns the ONE Playwright driver and the M accounts.

Each Account has its own browser context (login/profile) and its own list of
Worker tabs. The manager launches/stops contexts, tracks account state, and
pushes routing decisions into the MultiAccountPool. It is the single writer of
account state; the pool is the single arbiter of which tab a request runs on.

Correctness invariants enforced here:
- One driver, injected into every BrowserManager (M contexts, not M drivers).
- A Worker is created tagged with its account_id and never moves accounts.
- Cooldown/failure only flips schedulability (instant, race-free) — never
  reroutes an in-flight request.
- Stop drains (waits for in-flight) before closing a context.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from enum import Enum

from patchright.async_api import async_playwright

from src.core.browser.manager import AccountLaunchSpec, BrowserManager
from src.core.config import Config
from src.providers.base import get_provider
from src.accounts.registry import AccountConfig, AccountRegistry, registry_path, DEFAULT_ACCOUNT_ID
from src.accounts.usage import UsageStore, usage_path
from src.api.worker_pool import MultiAccountPool, Worker
from src.core.log import setup_logging

log = setup_logging("acct_manager")


class AccountState(str, Enum):
    DISABLED = "disabled"       # registered, not started
    STARTING = "starting"
    LOGGED_OUT = "logged_out"   # context up, needs login via noVNC
    ACTIVE = "active"           # serving requests
    NEARING = "nearing"         # serving, but approaching soft cap
    COOLDOWN = "cooldown"       # limit hit, temporarily not schedulable
    FAILED = "failed"           # login lost / repeated errors, needs attention


# States in which the account's tabs may serve new requests.
_SCHEDULABLE = {AccountState.ACTIVE, AccountState.NEARING}


@dataclass
class Account:
    cfg: AccountConfig
    state: AccountState = AccountState.DISABLED
    manager: BrowserManager | None = None
    workers: list = field(default_factory=list)
    started: bool = False
    last_error: str = ""
    # True when the overflow monitor (rather than normal startup or the user)
    # brought this account online, so it may later demote it when load subsides.
    auto_promoted: bool = False
    # Retry startup/manual-login detection, but never undo an explicit logout.
    login_pending: bool = False
    login_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class AccountManager:
    def __init__(self, registry: AccountRegistry, usage: UsageStore,
                 pool: MultiAccountPool, proxies=None) -> None:
        self._registry = registry
        self._usage = usage
        self._pool = pool
        self._proxies = proxies   # ProxyPool | None
        self._pw = None
        self._accounts: dict[str, Account] = {}
        self._lock = asyncio.Lock()  # serializes lifecycle ops (per-account heavy)

    def resolve_proxy(self, cfg: AccountConfig) -> dict | None:
        """The effective proxy for an account: explicit override, else pool slot.

        Explicit per-account proxy_server always wins. Otherwise the account's
        stable number picks a proxy from the pool (wrapping). Empty pool = direct.
        """
        if cfg.proxy_server:
            return Config.build_proxy(
                cfg.proxy_server, cfg.proxy_username, cfg.proxy_password, cfg.proxy_bypass
            )
        if self._proxies is not None:
            return self._proxies.proxy_for_number(cfg.number)
        return None

    def proxy_label(self, cfg: AccountConfig) -> str:
        """Human description of the effective proxy (redacted) for the UI."""
        if cfg.proxy_server:
            try:
                from urllib.parse import urlparse
                p = urlparse(cfg.proxy_server)
                return f"override: {p.hostname}:{p.port}"
            except Exception:
                return "override"
        if self._proxies is not None and self._proxies.count() > 0:
            return self._proxies.label_for_number(cfg.number)
        return "none (direct)"

    # ── Startup ──────────────────────────────────────────────────

    async def start_driver(self) -> None:
        self._pw = await async_playwright().start()

    async def stop_driver(self) -> None:
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    def _make_client(self, page, provider: str):
        """Client for THIS account's provider — chatgpt and gemini coexist."""
        return get_provider(provider).make_client(page)

    async def load_accounts(self) -> None:
        """Populate in-memory Account objects from the registry (no browsers yet)."""
        for cfg in self._registry.all():
            self._accounts[cfg.id] = Account(cfg=cfg)
            await self._pool.register_account(cfg.id, cfg.order)

    def _profile_dir(self, cfg: AccountConfig):
        sub = cfg.profile_subpath()
        return (Config.BROWSER_DATA_DIR / sub) if sub else Config.BROWSER_DATA_DIR

    async def _build_spec(self, cfg: AccountConfig) -> AccountLaunchSpec:
        proxy = self.resolve_proxy(cfg)
        if proxy:
            log.info(f"[{cfg.id}] using {self.proxy_label(cfg)}")
        return await AccountLaunchSpec.from_config(
            account_id=cfg.id,
            label=cfg.label or cfg.id,
            user_data_dir=self._profile_dir(cfg),
            proxy=proxy,
            timezone=cfg.timezone or None,
            provider=cfg.provider,
        )

    async def _navigate_with_retries(self, bm: BrowserManager, tries: int = 5) -> None:
        target = bm.provider_url()
        for attempt in range(1, tries + 1):
            try:
                await bm.navigate(target)
                return
            except Exception as e:
                log.warning(f"[{bm.account_id}] navigation attempt {attempt}/{tries} failed: {e}")
                if attempt == tries:
                    raise
                await asyncio.sleep(attempt * 4)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start_account(self, account_id: str) -> Account:
        """Launch the account's context + tabs. Idempotent. Never aborts the server.

        Sets ACTIVE if logged in, else LOGGED_OUT (context stays up so the user
        can sign in via noVNC; the monitor recognizes login automatically).
        """
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None:
                raise KeyError(account_id)
            async with acc.login_lock:
                if acc.started:
                    return acc
                acc.state = AccountState.STARTING
                n = acc.cfg.tabs or Config.MAX_CONCURRENT_REQUESTS
                try:
                    spec = await self._build_spec(acc.cfg)
                    bm = BrowserManager(spec, playwright=self._pw)
                    page = await bm.start()
                    await self._navigate_with_retries(bm)
                    await bm.apply_stealth_patches()
                    await asyncio.sleep(2)

                    prov = acc.cfg.provider
                    workers = [Worker(0, self._make_client(page, prov), page, bm,
                                      account_id=account_id, provider=prov)]
                    for i in range(1, n):
                        wp = await bm.new_worker_page(i)
                        workers.append(Worker(i, self._make_client(wp, prov), wp, bm,
                                              account_id=account_id, provider=prov))

                    logged_in = await bm.is_logged_in()
                    acc.manager = bm
                    acc.workers = workers
                    acc.started = True
                    acc.login_pending = True
                    acc.last_error = ""

                    await self._pool.register_account(account_id, acc.cfg.order, acc.cfg.provider)
                    # Decide schedulability BEFORE the tabs join the pool, so a
                    # logged-out account never serves a request in the gap.
                    await self._apply_login_result(acc, logged_in)
                    await self._pool.add_workers(account_id, workers)
                    log.info(
                        f"[{account_id}] started: {len(workers)} tab(s), "
                        f"{'logged in' if logged_in else 'NOT logged in — sign in via noVNC'}"
                    )
                except Exception as e:
                    # Degraded: one account failing must never take down the server.
                    acc.state = AccountState.FAILED
                    acc.last_error = f"{type(e).__name__}: {e}"
                    log.error(f"[{account_id}] start failed: {acc.last_error}")
                return acc

    async def stop_account(self, account_id: str, *, drain_timeout: float = 30.0) -> None:
        """Drain in-flight requests, then close the context. Keeps the profile."""
        async with self._lock:
            acc = self._accounts.get(account_id)
            if acc is None or not acc.started:
                return
            async with acc.login_lock:
                acc.login_pending = False
                await self._pool.retire_account(account_id)
                await self._pool.wait_drained(account_id, timeout=drain_timeout)
                try:
                    if acc.manager:
                        await acc.manager.close()
                except Exception as e:
                    log.warning(f"[{account_id}] close error: {e}")
                await self._pool.remove_account_slot(account_id)
                await self._pool.register_account(account_id, acc.cfg.order, acc.cfg.provider)  # keep an empty slot
                await self._pool.set_schedulable(account_id, False)
                acc.manager = None
                acc.workers = []
                acc.started = False
                acc.state = AccountState.DISABLED
                log.info(f"[{account_id}] stopped")

    async def open_login(self, account_id: str) -> "Account":
        """Put this account's tab on the provider's login page, raised to front.

        Starts the account first if it isn't running (you can't sign into a
        browser that isn't open). Routing is suspended while you're driving the
        tab by hand, so a request can't land on the page mid-login. Sign in via
        noVNC; login is detected automatically (the confirmation button is a fallback).
        """
        acc = self._accounts.get(account_id)
        if acc is None:
            raise KeyError(account_id)
        if not acc.started:
            await self.start_account(account_id)
            acc = self._accounts.get(account_id)
        if not acc.started or acc.manager is None:
            return acc

        async with acc.login_lock:
            if not acc.started or acc.manager is None:
                return acc
            acc.login_pending = True
            # Never serve requests on a tab the operator is typing into.
            await self._pool.set_schedulable(account_id, False)

            page = acc.workers[0].page if acc.workers else acc.manager.page
            try:
                await page.goto(acc.manager.provider_url(), wait_until="domcontentloaded")
                await page.bring_to_front()
            except Exception as e:
                log.warning(f"[{account_id}] could not open login page: {e}")

            # Maybe the stored session was still valid — then it's simply active.
            try:
                if await acc.manager.is_logged_in(page):
                    await self._apply_login_result(acc, True)
                    log.info(f"[{account_id}] already logged in")
                    return acc
            except Exception:
                pass
            acc.state = AccountState.LOGGED_OUT
            log.info(f"[{account_id}] login page opened — waiting for automatic login detection")
            return acc

    async def logout(self, account_id: str, *, drain_timeout: float = 20.0) -> "Account":
        """Sign this account out: stop routing, drain, clear cookies + storage.

        Keeps the browser context OPEN so you can immediately log back in via
        noVNC. In-flight requests are allowed to finish first — we never yank
        the session out from under a live request.
        """
        acc = self._accounts.get(account_id)
        if acc is None:
            raise KeyError(account_id)
        async with acc.login_lock:
            acc.login_pending = False
            if not acc.started or acc.manager is None:
                acc.state = AccountState.LOGGED_OUT
                return acc

            # 1. Stop routing, then let anything in flight finish.
            await self._pool.set_schedulable(account_id, False)
            await self._pool.wait_drained(account_id, timeout=drain_timeout)

            page = acc.workers[0].page if acc.workers else acc.manager.page

            # 2. Clear per-origin storage (must be ON the origin to do this).
            try:
                await page.goto(acc.manager.provider_url(), wait_until="domcontentloaded")
                await page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                )
            except Exception as e:
                log.warning(f"[{account_id}] storage clear failed: {e}")

            # 3. Clear cookies — context-wide, so this is the decisive logout.
            try:
                await acc.manager.context.clear_cookies()
            except Exception as e:
                log.warning(f"[{account_id}] clear_cookies failed: {e}")

            # 4. Put every tab back on a clean (now signed-out) page.
            for w in acc.workers:
                try:
                    await w.page.goto(acc.manager.provider_url(), wait_until="domcontentloaded")
                except Exception:
                    pass

            acc.state = AccountState.LOGGED_OUT
            acc.last_error = ""
            log.info(f"[{account_id}] logged out (cookies + storage cleared)")
            return acc

    async def _apply_login_result(self, acc: Account, ok: bool) -> None:
        """Called under the account's login lock; activation respects saved limits."""
        aid = acc.cfg.id
        was_waiting = acc.state in (AccountState.STARTING, AccountState.LOGGED_OUT)
        cap = acc.cfg.soft_cap or Config.ACCOUNT_SOFT_CAP
        if not ok:
            acc.state = AccountState.LOGGED_OUT
        elif self._usage.in_cooldown(aid) or self._usage.over_soft_cap(aid, cap):
            acc.state = AccountState.COOLDOWN
        else:
            acc.state = AccountState.NEARING if self._usage.nearing(aid, cap) else AccountState.ACTIVE
            acc.last_error = ""
        if ok and was_waiting:
            # Other tabs can still display a pre-login shell. Refresh them on
            # checkout, after the operator has finished signing in.
            for worker in acc.workers:
                worker.needs_fresh_chat = True
        await self._pool.set_schedulable(aid, acc.state in _SCHEDULABLE)

    async def confirm_login(self, account_id: str) -> bool:
        """Manual fallback; automatic checks use the same activation path."""
        acc = self._accounts.get(account_id)
        if acc is None:
            return False
        async with acc.login_lock:
            if not acc.started or acc.manager is None:
                return False
            acc.login_pending = True
            ok = await acc.manager.is_logged_in()
            await self._apply_login_result(acc, ok)
            return ok

    async def recheck_login(self, account_id: str) -> bool:
        acc = self._accounts.get(account_id)
        if acc is None or acc.login_lock.locked():
            return False
        async with acc.login_lock:
            if not (acc.started and acc.manager and acc.login_pending and
                    acc.cfg.enabled and acc.state == AccountState.LOGGED_OUT):
                return False
            ok = await acc.manager.is_logged_in(timeout_ms=900)
            # Config can change while the browser check is awaiting its DOM.
            if ok and acc.cfg.enabled and acc.login_pending and acc.state == AccountState.LOGGED_OUT:
                await self._apply_login_result(acc, True)
                log.info(f"[{account_id}] login detected automatically — {acc.state.value}")
                return True
            return False

    # ── State transitions (called by the monitor / request path) ──

    async def set_cooldown(self, account_id: str, until_ts: float, reason: str = "") -> None:
        acc = self._accounts.get(account_id)
        if acc is None:
            return
        async with acc.login_lock:
            acc.state = AccountState.COOLDOWN
            acc.last_error = reason
            await self._pool.set_schedulable(account_id, False)
            log.warning(f"[{account_id}] COOLDOWN ({reason})")

    async def mark_failed(self, account_id: str, reason: str = "") -> None:
        acc = self._accounts.get(account_id)
        if acc is None:
            return
        async with acc.login_lock:
            acc.login_pending = False
            acc.state = AccountState.FAILED
            acc.last_error = reason
            await self._pool.set_schedulable(account_id, False)
            log.warning(f"[{account_id}] FAILED ({reason})")

    async def reactivate(self, account_id: str) -> None:
        """Recheck after cooldown; delayed hydration is retried by the monitor."""
        acc = self._accounts.get(account_id)
        if acc is None:
            return
        async with acc.login_lock:
            if not acc.started or not acc.cfg.enabled or acc.manager is None or acc.state != AccountState.COOLDOWN:
                return
            acc.login_pending = True
            await self._apply_login_result(acc, await acc.manager.is_logged_in(timeout_ms=900))

    async def set_nearing(self, account_id: str, nearing: bool) -> None:
        acc = self._accounts.get(account_id)
        if acc is None:
            return
        if nearing and acc.state == AccountState.ACTIVE:
            acc.state = AccountState.NEARING  # still schedulable, just flagged
        elif not nearing and acc.state == AccountState.NEARING:
            acc.state = AccountState.ACTIVE

    # ── CRUD (admin API) ─────────────────────────────────────────

    async def create_account(self, cfg: AccountConfig) -> Account:
        await self._registry.add(cfg)
        acc = Account(cfg=cfg)
        self._accounts[cfg.id] = acc
        await self._pool.register_account(cfg.id, cfg.order)
        log.info(f"[{cfg.id}] created")
        return acc

    async def remove_account(self, account_id: str) -> None:
        if account_id == DEFAULT_ACCOUNT_ID:
            raise ValueError("the default account cannot be removed")
        await self.stop_account(account_id)
        acc = self._accounts.pop(account_id, None)
        await self._pool.remove_account_slot(account_id)
        await self._registry.remove(account_id)
        # Delete the profile dir (sub-account only; default lives at the root).
        if acc is not None:
            d = self._profile_dir(acc.cfg)
            if d != Config.BROWSER_DATA_DIR and d.exists():
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception as e:
                    log.warning(f"[{account_id}] profile dir cleanup failed: {e}")
        log.info(f"[{account_id}] removed")

    async def update_account(self, account_id: str, **changes) -> None:
        cfg = await self._registry.update(account_id, **changes)
        acc = self._accounts.get(account_id)
        if acc:
            acc.cfg = cfg
            await self._pool.set_order(account_id, cfg.order)

    # ── Queries ──────────────────────────────────────────────────

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def all(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: (a.cfg.order, a.cfg.id))

    def tier_order(self) -> list[str]:
        """Account ids in tier order (lowest order first)."""
        return [a.cfg.id for a in self.all()]

    async def close_all(self) -> None:
        for acc in list(self._accounts.values()):
            if acc.started and acc.manager:
                try:
                    await acc.manager.close()
                except Exception:
                    pass
        await self.stop_driver()
