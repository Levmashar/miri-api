"""
Account registry — the UI-editable, persisted list of accounts.

accounts.json lives under BROWSER_DATA_DIR (the mounted volume), so accounts
survive restarts. This module owns ONLY the persistent config of each account
(id, label, proxy, tabs, order, enabled) — NOT live runtime state (browser
contexts, worker lists, usage), which the AccountManager holds in memory.

All writes go through one async lock and land atomically via os.replace, so a
crash mid-write can never truncate the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("acct_registry")

# Account ids become directory names (_accounts/<id>) and pkill patterns, so they
# are strictly validated: no path traversal, no regex/shell metacharacters.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# The legacy single-account login lives at the volume root, not under _accounts/.
DEFAULT_ACCOUNT_ID = "default"


@dataclass
class AccountConfig:
    """Persistent config for one account (a row in accounts.json)."""
    id: str
    label: str = ""
    # Which service this account signs into. Providers run SIMULTANEOUSLY — this
    # is a property of the account, not a global switch, so chatgpt and gemini
    # accounts coexist and a request only ever routes to its own provider.
    provider: str = "chatgpt"
    enabled: bool = True
    order: int = 100                 # lower = higher priority (served first)
    # Stable 1-indexed account number. Assigned on creation and kept forever, so
    # its proxy-pool slot (proxy = pool[(number-1) mod len]) never shifts when
    # other accounts are removed. 0 means "assign the next free number".
    number: int = 0
    tabs: int = 0                    # 0 = use Config.MAX_CONCURRENT_REQUESTS
    soft_cap: int = 0                # 0 = use Config.ACCOUNT_SOFT_CAP
    # Explicit per-account proxy OVERRIDE. Empty server = fall back to the proxy
    # pool assigned by `number`; if the pool is also empty, connect direct.
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_bypass: str = "localhost,127.0.0.1,::1"
    timezone: str = ""               # "" = Config.BROWSER_TIMEZONE; may be "auto"

    def profile_subpath(self) -> str:
        """Path of this account's profile relative to BROWSER_DATA_DIR.

        The default account keeps the legacy layout (volume root) so the
        existing login is never moved/lost; every other account is isolated
        under _accounts/<id> so no account dir is an ancestor of another.
        """
        return "" if self.id == DEFAULT_ACCOUNT_ID else f"_accounts/{self.id}"

    def redacted(self) -> dict:
        """Dict for API responses — never leaks the proxy password."""
        d = asdict(self)
        if d.get("proxy_password"):
            d["proxy_password"] = "***"
        d["has_proxy"] = bool(self.proxy_server)
        return d


def validate_id(account_id: str) -> str:
    account_id = (account_id or "").strip()
    if not _ID_RE.match(account_id):
        raise ValueError(
            "account id must be 1-40 chars of letters, digits, '-' or '_' "
            f"(got {account_id!r})"
        )
    return account_id


class AccountRegistry:
    """Loads/saves accounts.json. In-memory dict of id -> AccountConfig."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._accounts: dict[str, AccountConfig] = {}
        self._lock = asyncio.Lock()

    def _load_unlocked(self) -> None:
        self._accounts = {}
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"accounts.json unreadable ({e}); starting empty")
            return
        for row in raw.get("accounts", []):
            try:
                # Ignore unknown keys so an older/newer file still loads.
                known = {k: row[k] for k in AccountConfig.__dataclass_fields__ if k in row}
                cfg = AccountConfig(**known)
                cfg.id = validate_id(cfg.id)
                self._accounts[cfg.id] = cfg
            except Exception as e:
                log.warning(f"skipping bad account row {row!r}: {e}")

    def _save_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"accounts": [asdict(a) for a in self._accounts.values()]}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic

    async def load(self) -> None:
        async with self._lock:
            self._load_unlocked()

    def _next_number(self) -> int:
        used = {a.number for a in self._accounts.values() if a.number > 0}
        n = 1
        while n in used:
            n += 1
        return n

    def suggest_id(self, provider: str) -> str:
        """A fresh, unique, valid account id for a provider (e.g. 'seedream-2').

        Ids are auto-assigned now (the UI no longer asks for one), so this mints a
        provider-prefixed, 1-indexed slug that doesn't collide with an existing id
        and always satisfies validate_id()."""
        base = re.sub(r"[^a-z0-9]+", "", (provider or "acct").lower()) or "acct"
        existing = set(self._accounts)
        n = 1
        while f"{base}-{n}" in existing:
            n += 1
        return f"{base}-{n}"

    async def ensure_default(self) -> None:
        """Seed a 'default' account so the existing volume-root login is used.

        Non-lossy: if accounts.json already lists 'default' we leave it. This is
        what preserves the current single-account login on first multi-account boot.
        """
        async with self._lock:
            changed = False
            if DEFAULT_ACCOUNT_ID not in self._accounts:
                self._accounts[DEFAULT_ACCOUNT_ID] = AccountConfig(
                    id=DEFAULT_ACCOUNT_ID, label="Default", order=0, number=1
                )
                changed = True
                log.info("Seeded 'default' account (existing login preserved)")
            # Backfill numbers for any account created before numbering existed.
            for cfg in self._accounts.values():
                if cfg.number <= 0:
                    cfg.number = 1 if cfg.id == DEFAULT_ACCOUNT_ID else self._next_number()
                    changed = True
            if changed:
                self._save_unlocked()

    def all(self) -> list[AccountConfig]:
        """Enabled and disabled, sorted by (order, id)."""
        return sorted(self._accounts.values(), key=lambda a: (a.order, a.id))

    def get(self, account_id: str) -> AccountConfig | None:
        return self._accounts.get(account_id)

    async def add(self, cfg: AccountConfig) -> AccountConfig:
        cfg.id = validate_id(cfg.id)
        async with self._lock:
            if cfg.id in self._accounts:
                raise ValueError(f"account {cfg.id!r} already exists")
            if cfg.number <= 0:
                cfg.number = self._next_number()
            self._accounts[cfg.id] = cfg
            self._save_unlocked()
        return cfg

    async def update(self, account_id: str, **changes) -> AccountConfig:
        async with self._lock:
            cfg = self._accounts.get(account_id)
            if cfg is None:
                raise KeyError(account_id)
            for k, v in changes.items():
                if k in AccountConfig.__dataclass_fields__ and k != "id":
                    setattr(cfg, k, v)
            self._save_unlocked()
            return cfg

    async def remove(self, account_id: str) -> None:
        async with self._lock:
            self._accounts.pop(account_id, None)
            self._save_unlocked()


def registry_path() -> Path:
    return Config.BROWSER_DATA_DIR / "accounts.json"
