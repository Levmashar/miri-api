"""
Account monitor — the background loop that drives auto-switch and overflow.

Runs every few seconds and:
1. Expires cooldowns: a COOLDOWN account whose hard timer has passed AND whose
   soft-cap window has drained is reactivated (re-verifying login first).
2. Enforces the soft cap: an ACTIVE account over its soft cap is put on cooldown
   proactively so traffic rotates to the next account BEFORE the hard wall.
3. Flags "nearing".
4. Overflow activation: if a disabled/not-yet-started account remains, sustained
   saturation can start it. At normal startup all enabled accounts are already
   started, so this is mainly a recovery path after an operator stops an account.

All state changes go through AccountManager (single writer). The monitor never
touches the pool or an in-flight request directly.
"""

from __future__ import annotations

import asyncio
import time

from src.accounts.manager import AccountManager, AccountState
from src.accounts.usage import UsageStore
from src.api.worker_pool import MultiAccountPool
from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("acct_monitor")


class AccountMonitor:
    def __init__(self, manager: AccountManager, usage: UsageStore, pool: MultiAccountPool) -> None:
        self._mgr = manager
        self._usage = usage
        self._pool = pool
        self._saturated_since: float | None = None
        self._slack_since: float | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        log.info("Account monitor started")
        while True:
            try:
                await asyncio.sleep(3)
                now = time.time()
                await self._tick(now)
                await self._usage.save()
            except asyncio.CancelledError:
                log.info("Account monitor stopped")
                raise
            except Exception as e:
                log.error(f"monitor tick failed (continuing): {e}", exc_info=True)

    async def _tick(self, now: float) -> None:
        # ── 1+2+3: per-account state maintenance ──
        for acc in self._mgr.all():
            if not acc.started:
                continue
            aid = acc.cfg.id
            soft_cap = acc.cfg.soft_cap or Config.ACCOUNT_SOFT_CAP

            if acc.state == AccountState.COOLDOWN:
                hard = self._usage.in_cooldown(aid, now)
                soft = self._usage.over_soft_cap(aid, soft_cap, now)
                if not hard and not soft:
                    await self._mgr.reactivate(aid)
            elif acc.state in (AccountState.ACTIVE, AccountState.NEARING):
                if self._usage.in_cooldown(aid, now):
                    await self._mgr.set_cooldown(aid, self._usage._get(aid).cooldown_until, "limit")
                elif self._usage.over_soft_cap(aid, soft_cap, now):
                    await self._mgr.set_cooldown(aid, 0, "soft cap reached")
                else:
                    await self._mgr.set_nearing(aid, self._usage.nearing(aid, soft_cap, now))

        # Restored SPAs may still show the anonymous shell on startup.
        # Checks are read-only, bounded, and independent across accounts.
        checks = [self._mgr.recheck_login(acc.cfg.id) for acc in self._mgr.all()
                  if acc.started and acc.login_pending and acc.state == AccountState.LOGGED_OUT]
        if checks:
            results = await asyncio.gather(*checks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    log.warning("Automatic login check failed (will retry): %s", result)

        # ── 4: overflow activation / shrink ──
        await self._manage_overflow(now)

    async def _manage_overflow(self, now: float) -> None:
        saturated = self._pool.saturated()

        # EXPAND: sustained saturation -> start the next not-started account.
        if saturated:
            self._slack_since = None
            if self._saturated_since is None:
                self._saturated_since = now
            elif now - self._saturated_since >= Config.OVERFLOW_EXPAND_HOLD_S:
                await self._promote_next()
                self._saturated_since = now  # reset the timer after acting
        else:
            self._saturated_since = None
            # SHRINK: sustained slack -> stop one auto-promoted account.
            if self._slack_since is None:
                self._slack_since = now
            elif now - self._slack_since >= Config.OVERFLOW_SHRINK_HOLD_S:
                await self._demote_one()
                self._slack_since = now

    async def _promote_next(self) -> None:
        for acc in self._mgr.all():
            if acc.started:
                continue
            if not acc.cfg.enabled:
                continue
            log.info(f"Overflow: promoting account '{acc.cfg.id}' to relieve load")
            acc.auto_promoted = True
            await self._mgr.start_account(acc.cfg.id)
            return  # one at a time

    async def _demote_one(self) -> None:
        # Stop the highest-order auto-promoted account (last brought online).
        for acc in reversed(self._mgr.all()):
            if acc.started and acc.auto_promoted:
                log.info(f"Overflow: load subsided — stopping auto-promoted '{acc.cfg.id}'")
                acc.auto_promoted = False
                await self._mgr.stop_account(acc.cfg.id)
                return
