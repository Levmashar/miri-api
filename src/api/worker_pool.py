"""
Multi-account worker pool — runs requests across M accounts x N tabs.

A Worker is one browser TAB (page + client + conversation state), PERMANENTLY
tagged with its account_id and never reassigned to another account. A request
checks out exactly one Worker for its whole life and returns it; two requests
never share a Worker, and a Worker never migrates between accounts. Those two
facts are the entire no-mixing guarantee at this layer (the clipboard — the one
resource shared across accounts — is guarded separately by src/clipboard_lock.py).

Routing:
- acquire() with no account: FAIR account-level round robin. Requests for a
  provider/model rotate through every schedulable account, independent of how
  many tabs each account owns. Busy accounts are skipped so idle capacity is
  still used.
- acquire(account_id=X): PINNED. Return a tab from account X only. Used for
  conversation chaining: a /c/<id> thread's cookies live in ONE account's
  profile, so a continuation MUST run on that same account. If X is not
  schedulable, raise AccountUnavailable (-> HTTP 409) — NEVER reroute a chain.

Everything (deques, schedulable flags, waiter count) is guarded by ONE
asyncio.Condition so state transitions (cooldown, drain, activation) can't race
an acquire.
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from inspect import isawaitable

from src.core.log import setup_logging
from src.providers.temporary_chat import (
    fresh_chat_required,
    required as temporary_required,
)
from fastapi import HTTPException

log = setup_logging("worker_pool")

_request_excluded_accounts = ContextVar("request_excluded_accounts", default=frozenset())
_last_failed_account = ContextVar("last_failed_account", default=None)


def request_excluded_accounts() -> frozenset[str]:
    """Accounts excluded by the current request's failover attempt."""
    return _request_excluded_accounts.get()


def set_request_excluded_accounts(accounts) -> object:
    return _request_excluded_accounts.set(frozenset(accounts))


def reset_request_excluded_accounts(token) -> None:
    _request_excluded_accounts.reset(token)


def last_failed_account() -> str | None:
    return _last_failed_account.get()


def reset_last_failed_account() -> object:
    return _last_failed_account.set(None)


def restore_last_failed_account(token) -> None:
    _last_failed_account.reset(token)


def is_retryable_error(error: BaseException | None) -> bool:
    if error is None or isinstance(error, (BrowserBusy, AccountUnavailable)):
        return False
    if isinstance(error, HTTPException):
        return error.status_code == 429 or error.status_code >= 500
    return isinstance(error, Exception)


class BrowserBusy(Exception):
    """All schedulable tabs are busy and the queue is full / timed out. -> 429."""


class AccountUnavailable(Exception):
    """A pinned (chained) request's owning account is not schedulable. -> 409."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(
            f"The account that owns this conversation ({account_id}) is currently "
            "unavailable (cooldown/failed/disabled). Start a new conversation."
        )


class Worker:
    """One tab: a page, a client, its conversation state, and its account_id."""

    MAX_THREAD_MESSAGES = 8  # start a new chat after this many messages (UI degrades)
    MIN_MESSAGE_GAP = 3.0    # min seconds between messages on this tab

    def __init__(self, index, client, page, browser, account_id: str,
                 provider: str = "chatgpt") -> None:
        self.index = index
        self.client = client
        self.page = page
        self.browser = browser
        self.account_id = account_id
        # Immutable, like account_id: a tab belongs to ONE provider forever, so a
        # chatgpt request can never be served by a gemini tab.
        self.provider = provider
        self.thread_message_count = 0
        self.last_response_time = 0.0
        self.limit_hit = False
        # Set True when the tab's account is being torn down. Re-checked AFTER
        # checkout so a request that dequeued a doomed tab bails instead of
        # driving a closing page.
        self.retired = False

    def extract_thread_id(self) -> str:
        return self.client._extract_thread_id()

    async def navigate_to_thread(self, thread_id: str) -> None:
        if temporary_required() or fresh_chat_required():
            raise HTTPException(409, detail="New-chat-per-request mode cannot resume saved threads. Send the context in messages instead.")
        await self.client.navigate_to_thread(thread_id)

    async def recover(self) -> bool:
        return await self.browser.recover_page(self.page)

    def increment_thread_count(self) -> None:
        self.thread_message_count += 1
        self.last_response_time = time.time()

    async def ensure_fresh_chat(self, keep_thread: bool = False) -> None:
        """Cooldown between messages, and rotate to a new chat when the tab is full.

        keep_thread=True suppresses rotation for conversation chaining, which must
        stay in the same thread.
        """
        if self.last_response_time > 0:
            elapsed = time.time() - self.last_response_time
            if elapsed < self.MIN_MESSAGE_GAP:
                await asyncio.sleep(self.MIN_MESSAGE_GAP - elapsed)

        if keep_thread:
            if self.thread_message_count >= self.MAX_THREAD_MESSAGES:
                log.warning(
                    f"[{self.account_id}] continuing a chained conversation at "
                    f"{self.thread_message_count} messages (past the "
                    f"{self.MAX_THREAD_MESSAGES}-message degradation point)."
                )
            return

        if self.thread_message_count < self.MAX_THREAD_MESSAGES:
            return

        try:
            await self.client.new_chat()
            self.thread_message_count = 0
        except Exception as e:
            log.warning(f"[{self.account_id}] new_chat() failed, retrying once: {e}")
            try:
                await asyncio.sleep(2)
                await self.client.new_chat()
                self.thread_message_count = 0
            except Exception as e2:
                log.error(f"[{self.account_id}] new_chat() retry failed: {e2}")


class _AccountSlot:
    def __init__(self, account_id: str, order: int, provider: str = "chatgpt") -> None:
        self.account_id = account_id
        self.order = order
        self.provider = provider
        self.schedulable = True          # False = don't route new requests here
        self.workers: list[Worker] = []  # all tabs of this account
        self.idle: list[Worker] = []     # currently free tabs (a stack)
        self.checked_out = 0             # tabs currently serving a request


class MultiAccountPool:
    """Checks tabs out to requests across accounts, with a bounded wait queue."""

    def __init__(self, *, max_waiters: int = 8, acquire_timeout: float = 240.0) -> None:
        self._slots: dict[str, _AccountSlot] = {}
        self._cond = asyncio.Condition()
        self._next_account: dict[tuple[str, str], str] = {}
        self._failure_handler = None
        self._waiters = 0
        self._max_waiters = max_waiters
        self._acquire_timeout = acquire_timeout

    # ── Composition (called by AccountManager under normal control flow) ──

    async def register_account(self, account_id: str, order: int,
                               provider: str = "chatgpt") -> None:
        async with self._cond:
            slot = self._slots.get(account_id)
            if slot is None:
                self._slots[account_id] = _AccountSlot(account_id, order, provider)
            else:
                slot.order = order
                slot.provider = provider

    async def set_order(self, account_id: str, order: int) -> None:
        async with self._cond:
            if account_id in self._slots:
                self._slots[account_id].order = order

    async def set_schedulable(self, account_id: str, schedulable: bool) -> None:
        """Flip whether new requests route to this account. Instant failover.

        Does NOT touch in-flight requests (they already hold their tab) or close
        anything — it just removes the account from the routing scan.
        """
        async with self._cond:
            slot = self._slots.get(account_id)
            if slot is not None:
                slot.schedulable = schedulable
                if schedulable:
                    self._cond.notify_all()

    def set_failure_handler(self, handler) -> None:
        """Set the control-plane callback used to reflect routing failures."""
        self._failure_handler = handler

    async def report_account_failure(self, account_id: str, error: BaseException) -> None:
        """Stop routing to a broken account before retrying another account."""
        await self.set_schedulable(account_id, False)
        if self._failure_handler is None:
            return
        try:
            result = self._failure_handler(
                account_id, f"{type(error).__name__}: {str(error)[:300]}"
            )
            if isawaitable(result):
                await result
        except Exception as callback_error:
            log.warning(
                f"[{account_id}] could not reflect request failure in account state: "
                f"{callback_error}"
            )

    async def add_workers(self, account_id: str, workers: list[Worker]) -> None:
        """Bring an account's tabs online (activation)."""
        async with self._cond:
            slot = self._slots.setdefault(account_id, _AccountSlot(account_id, 100))
            for w in workers:
                w.retired = False
                slot.workers.append(w)
                slot.idle.append(w)
            self._cond.notify_all()

    async def retire_account(self, account_id: str) -> None:
        """Stop routing to an account and mark its tabs retired.

        Idle tabs become unpickable immediately; checked-out tabs finish their
        in-flight request, then drop on release. Does NOT close anything and does
        NOT remove the slot — call wait_drained() then remove_account_slot().
        """
        async with self._cond:
            slot = self._slots.get(account_id)
            if slot is None:
                return
            slot.schedulable = False
            for w in slot.workers:
                w.retired = True
            slot.idle.clear()
            self._cond.notify_all()

    async def wait_drained(self, account_id: str, timeout: float = 30.0) -> bool:
        """Wait until no tab of the account is mid-request. True if fully drained."""
        deadline = time.monotonic() + timeout
        async with self._cond:
            while True:
                slot = self._slots.get(account_id)
                if slot is None or slot.checked_out == 0:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return slot.checked_out == 0

    async def remove_account_slot(self, account_id: str) -> None:
        async with self._cond:
            self._slots.pop(account_id, None)

    # ── Acquire / release ────────────────────────────────────────

    def _pick_idle(self, pinned, provider=None, model=None, excluded_accounts=()):
        excluded = set(excluded_accounts)
        if pinned is not None:
            slot = self._slots.get(pinned)
            if slot is None or not slot.schedulable or pinned in excluded:
                return None
            # A pinned (chained) request must also match the provider it asked
            # for: resuming a chatgpt thread on a gemini tab is nonsense.
            if provider is not None and slot.provider != provider:
                return None
            while slot.idle:
                w = slot.idle.pop()
                if not w.retired:
                    return w
            return None
        # Fair account-level round robin within the requested provider/model.
        slots = [
            slot for slot in sorted(self._slots.values(), key=lambda s: (s.order, s.account_id))
            if provider is None or slot.provider == provider
        ]
        if not slots:
            return None
        cursor_key = (provider or "", model or "")
        next_account = self._next_account.get(cursor_key)
        start = next(
            (i for i, slot in enumerate(slots) if slot.account_id == next_account),
            0,
        )
        for offset in range(len(slots)):
            index = (start + offset) % len(slots)
            slot = slots[index]
            if not slot.schedulable or slot.account_id in excluded:
                continue
            while slot.idle:
                w = slot.idle.pop()
                if not w.retired:
                    self._next_account[cursor_key] = slots[(index + 1) % len(slots)].account_id
                    return w
        return None

    def _is_schedulable(self, account_id, provider=None, excluded_accounts=()):
        slot = self._slots.get(account_id)
        if account_id in set(excluded_accounts) or not (slot and slot.schedulable and slot.workers):
            return False
        return provider is None or slot.provider == provider

    async def _acquire(self, pinned, provider=None, model=None, excluded_accounts=()):
        deadline = time.monotonic() + self._acquire_timeout
        async with self._cond:
            while True:
                w = self._pick_idle(pinned, provider, model, excluded_accounts)
                if w is not None:
                    slot = self._slots.get(w.account_id)
                    if slot is not None:
                        slot.checked_out += 1
                    return w

                # A pinned (chained) request whose account can never serve must
                # fail fast with 409 — never wait, never reroute to another account.
                if pinned is not None and not self._is_schedulable(pinned, provider, excluded_accounts):
                    raise AccountUnavailable(pinned)

                # No tab of this provider exists at all -> waiting is pointless.
                if provider is not None and not any(
                    sl.schedulable and sl.workers and sl.provider == provider
                    and sl.account_id not in excluded_accounts
                    for sl in self._slots.values()
                ):
                    raise BrowserBusy(
                        "No signed-in '" + provider + "' account is available. "
                        "Add or log into one in the admin panel."
                    )
                if self._waiters >= self._max_waiters:
                    log.warning(f"Queue full ({self._waiters} waiting) — shedding")
                    raise BrowserBusy(
                        f"All browser tabs are busy and {self._waiters} request(s) "
                        "are already queued."
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserBusy("Timed out waiting for a free browser tab.")

                self._waiters += 1
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise BrowserBusy("Timed out waiting for a free browser tab.")
                finally:
                    self._waiters -= 1
                # loop: re-scan under the lock

    async def _release(self, worker: Worker) -> None:
        async with self._cond:
            slot = self._slots.get(worker.account_id)
            if slot is not None:
                slot.checked_out = max(0, slot.checked_out - 1)
                # Drop retired tabs (account being torn down) — never re-idle them.
                if not worker.retired and worker in slot.workers:
                    slot.idle.append(worker)
            self._cond.notify_all()

    def acquire(self, account_id=None, provider=None, model=None, excluded_accounts=()):
        """async with pool.acquire(account_id, provider) as worker.

        provider scopes the search so a request only ever gets a tab of the
        service it asked for.
        """
        return _Acquired(self, account_id, provider, model, excluded_accounts)

    # ── Introspection ────────────────────────────────────────────

    def size(self) -> int:
        return sum(len(s.workers) for s in self._slots.values())

    def schedulable_size(self) -> int:
        return sum(len(s.workers) for s in self._slots.values() if s.schedulable)

    def idle_count(self) -> int:
        return sum(len(s.idle) for s in self._slots.values() if s.schedulable)

    def queue_depth(self) -> int:
        return self._waiters

    def saturated(self) -> bool:
        """True when every schedulable tab is busy AND requests are queued."""
        return self.idle_count() == 0 and self._waiters > 0

    def provider_stats(self, provider: str) -> dict:
        """Tab counts for one provider (an independent capacity pool)."""
        slots = [s for s in self._slots.values() if s.provider == provider]
        return {
            "tabs": sum(len(s.workers) for s in slots),
            "schedulable": sum(len(s.workers) for s in slots if s.schedulable),
            "idle": sum(len(s.idle) for s in slots if s.schedulable),
        }

    def saturated_for(self, provider: str) -> bool:
        st = self.provider_stats(provider)
        return st["schedulable"] > 0 and st["idle"] == 0 and self._waiters > 0

    def providers_live(self) -> list:
        return sorted({s.provider for s in self._slots.values() if s.workers})

    def account_ids(self) -> list[str]:
        return [s.account_id for s in sorted(self._slots.values(), key=lambda s: (s.order, s.account_id))]

    async def try_acquire_nowait(self, account_id: str | None = None) -> Worker | None:
        """Grab an idle tab without waiting (for /status). Caller must release."""
        async with self._cond:
            return self._pick_idle(account_id, None)

    async def release_nowait(self, worker: Worker) -> None:
        await self._release(worker)


class _Acquired:
    def __init__(self, pool, account_id=None, provider=None, model=None, excluded_accounts=()):
        self._pool = pool
        self._account_id = account_id
        self._provider = provider
        self._model = model
        self._excluded_accounts = tuple(
            set(excluded_accounts) | set(request_excluded_accounts())
        )
        self.worker = None

    async def __aenter__(self):
        self.worker = await self._pool._acquire(
            self._account_id, self._provider, self._model, self._excluded_accounts
        )
        try:
            self.worker.limit_hit = False
            if fresh_chat_required() or getattr(self.worker, 'needs_fresh_chat', False):
                await self.worker.client.new_chat()
                self.worker.thread_message_count = 0
                self.worker.needs_fresh_chat = False
        except BaseException as error:
            self.worker.needs_fresh_chat = True
            if is_retryable_error(error):
                _last_failed_account.set(self.worker.account_id)
                await self._pool.report_account_failure(self.worker.account_id, error)
            await self._pool._release(self.worker)
            self.worker = None
            raise
        return self.worker

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.worker is not None:
            try:
                if is_retryable_error(exc):
                    _last_failed_account.set(self.worker.account_id)
                    if not self.worker.limit_hit:
                        await self._pool.report_account_failure(self.worker.account_id, exc)
                if temporary_required():
                    # Downloads/extraction have finished; discard the temporary
                    # page while this tab is still exclusively checked out.
                    from src.providers.base import get_provider
                    await self.worker.page.goto(get_provider(self.worker.provider).url,
                                                wait_until="domcontentloaded", timeout=15000)
                    self.worker.thread_message_count = 0
            except Exception as cleanup_error:
                log.warning(f"Temporary chat cleanup failed: {cleanup_error}")
                # Force a fresh chat before this tab's next normal request.
                self.worker.thread_message_count = self.worker.MAX_THREAD_MESSAGES
                self.worker.needs_fresh_chat = True
            finally:
                await self._pool._release(self.worker)
