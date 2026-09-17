"""
Per-account usage accounting.

Tracks, per account: a rolling count of requests in the soft-cap window, total
requests, successes/failures, limit-hit history, and the current cooldown-until.
Everything here is an OBSERVED ESTIMATE from our own request stream — never a
real "remaining quota" number, which the web UI does not expose.

Persisted to usage.json (atomic) so counters survive a restart. Time is passed
in by callers (monotonic-ish wall clock) so the module has no hidden clock —
important because Date.now() is unavailable in some execution contexts.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("acct_usage")


@dataclass
class AccountUsage:
    account_id: str
    requests_total: int = 0
    successes: int = 0
    failures: int = 0
    limit_hits: int = 0
    last_limit_ts: float = 0.0
    last_request_ts: float = 0.0
    cooldown_until: float = 0.0
    # timestamps of recent requests, for the rolling soft-cap window
    _window: deque = field(default_factory=deque)

    def prune(self, now: float, window_s: float) -> None:
        cutoff = now - window_s
        w = self._window
        while w and w[0] < cutoff:
            w.popleft()

    def window_count(self, now: float, window_s: float) -> int:
        self.prune(now, window_s)
        return len(self._window)

    def to_json(self) -> dict:
        return {
            "account_id": self.account_id,
            "requests_total": self.requests_total,
            "successes": self.successes,
            "failures": self.failures,
            "limit_hits": self.limit_hits,
            "last_limit_ts": self.last_limit_ts,
            "cooldown_until": self.cooldown_until,
        }


class UsageStore:
    """Holds AccountUsage for every account; persists periodically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._usage: dict[str, AccountUsage] = {}
        self._lock = asyncio.Lock()
        self._dirty = False

    def _get(self, account_id: str) -> AccountUsage:
        u = self._usage.get(account_id)
        if u is None:
            u = AccountUsage(account_id=account_id)
            self._usage[account_id] = u
        return u

    async def load(self) -> None:
        async with self._lock:
            if not self._path.exists():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"usage.json unreadable ({e}); starting fresh")
                return
            for aid, row in raw.get("usage", {}).items():
                u = AccountUsage(account_id=aid)
                for k in ("requests_total", "successes", "failures", "limit_hits",
                          "last_limit_ts", "cooldown_until"):
                    if k in row:
                        setattr(u, k, row[k])
                self._usage[aid] = u

    async def save(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"usage": {aid: u.to_json() for aid, u in self._usage.items()}}
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
            self._dirty = False

    # ── Observations (called from the request path) ──────────────

    def note_request(self, account_id: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        u = self._get(account_id)
        u.requests_total += 1
        u.last_request_ts = now
        u._window.append(now)
        # Bound the window to the soft-cap period on every request. Otherwise, with
        # the soft cap disabled (the default), nothing ever prunes it — the pruners
        # live behind over_soft_cap()/nearing() which early-return when cap<=0 — so
        # the deque would grow one entry per request forever for each active account.
        u.prune(now, Config.ACCOUNT_SOFT_CAP_WINDOW_MIN * 60)
        self._dirty = True

    def note_success(self, account_id: str) -> None:
        self._get(account_id).successes += 1
        self._dirty = True

    def note_failure(self, account_id: str) -> None:
        self._get(account_id).failures += 1
        self._dirty = True

    def note_limit(self, account_id: str, reset_seconds: float, now: float | None = None) -> float:
        """Record a hard limit hit; return the cooldown-until timestamp."""
        now = now if now is not None else time.time()
        u = self._get(account_id)
        u.limit_hits += 1
        u.last_limit_ts = now
        cooldown = reset_seconds if reset_seconds > 0 else Config.ACCOUNT_DEFAULT_COOLDOWN_MIN * 60
        u.cooldown_until = now + cooldown
        self._dirty = True
        return u.cooldown_until

    # ── Queries ─────────────────────────────────────────────────

    def snapshot(self, account_id: str, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        u = self._get(account_id)
        window_s = Config.ACCOUNT_SOFT_CAP_WINDOW_MIN * 60
        in_window = u.window_count(now, window_s)
        cooldown_remaining = max(0.0, u.cooldown_until - now)
        return {
            "account_id": account_id,
            "requests_total": u.requests_total,
            "successes": u.successes,
            "failures": u.failures,
            "limit_hits": u.limit_hits,
            "requests_in_window": in_window,
            "cooldown_remaining_s": round(cooldown_remaining),
            # explicitly an estimate — NOT remaining quota
            "estimate_only": True,
        }

    def in_cooldown(self, account_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self._get(account_id).cooldown_until > now

    def over_soft_cap(self, account_id: str, soft_cap: int, now: float | None = None) -> bool:
        if soft_cap <= 0:
            return False
        now = now if now is not None else time.time()
        window_s = Config.ACCOUNT_SOFT_CAP_WINDOW_MIN * 60
        return self._get(account_id).window_count(now, window_s) >= soft_cap

    def nearing(self, account_id: str, soft_cap: int, now: float | None = None) -> bool:
        if soft_cap <= 0:
            return False
        now = now if now is not None else time.time()
        window_s = Config.ACCOUNT_SOFT_CAP_WINDOW_MIN * 60
        threshold = soft_cap * Config.ACCOUNT_NEARING_FRACTION
        return self._get(account_id).window_count(now, window_s) >= threshold


def usage_path() -> Path:
    return Config.BROWSER_DATA_DIR / "usage.json"
