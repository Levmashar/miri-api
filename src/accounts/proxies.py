"""
Proxy pool — an ordered, editable list of proxies, assigned to accounts by number.

The operator maintains one list (edited as text, one proxy per line, in the
admin/noVNC panel). Assignment is by ACCOUNT NUMBER, 1-indexed and stable:

    account number N  ->  proxy at index (N-1) mod len(pool)

so account 1 uses proxy 1, account 2 uses proxy 2, and when there are fewer
proxies than accounts it wraps back to proxy 1. Because the number is stable per
account, an account always keeps the same proxy slot even if other accounts are
removed.

Each line is a standard proxy URL: scheme://[user:pass@]host:port
(http/https/socks5/socks4). Lines are validated with Config.build_proxy, so a
malformed proxy is rejected up front rather than silently launching direct.

Persisted atomically to proxies.json under BROWSER_DATA_DIR.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from src.core.config import Config
from src.core.log import setup_logging

log = setup_logging("proxy_pool")


class ProxyPool:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lines: list[str] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            self._lines = []
            if not self._path.exists():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._lines = [str(x).strip() for x in raw.get("proxies", []) if str(x).strip()]
            except Exception as e:
                log.error(f"proxies.json unreadable ({e}); starting empty")

    def _save_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"proxies": self._lines}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _validate(line: str) -> None:
        """Raise ValueError if `line` is not a usable proxy URL."""
        # build_proxy does the full scheme/port/SOCKS-auth validation.
        Config.build_proxy(line)

    async def replace(self, lines: list[str]) -> None:
        """Replace the whole list (from the textarea). Validates every line."""
        cleaned = [ln.strip() for ln in lines if ln and ln.strip()]
        for i, ln in enumerate(cleaned, 1):
            try:
                self._validate(ln)
            except ValueError as e:
                raise ValueError(f"proxy #{i} invalid: {e}")
        async with self._lock:
            self._lines = cleaned
            self._save_unlocked()
        log.info(f"Proxy pool updated: {len(cleaned)} prox(ies)")

    def count(self) -> int:
        return len(self._lines)

    def raw_lines(self) -> list[str]:
        """The list as entered — includes credentials (admin-only surface)."""
        return list(self._lines)

    def redacted_lines(self) -> list[str]:
        """The list with passwords masked, for display where creds shouldn't show."""
        out = []
        for ln in self._lines:
            try:
                p = urlparse(ln)
                if p.password:
                    netloc = f"{p.username}:***@{p.hostname}:{p.port}"
                    out.append(f"{p.scheme}://{netloc}")
                    continue
            except Exception:
                pass
            out.append(ln)
        return out

    def line_for_number(self, number: int) -> str | None:
        """The raw proxy line assigned to account `number` (1-indexed), or None."""
        if not self._lines or number < 1:
            return None
        return self._lines[(number - 1) % len(self._lines)]

    def proxy_for_number(self, number: int) -> dict | None:
        """The Playwright proxy dict assigned to account `number`, or None."""
        line = self.line_for_number(number)
        if not line:
            return None
        try:
            return Config.build_proxy(line)
        except ValueError as e:
            log.warning(f"assigned proxy for account #{number} is invalid ({e}); direct")
            return None

    def label_for_number(self, number: int) -> str:
        """Human label of the assigned proxy (redacted), e.g. 'proxy 2: host:port'."""
        if not self._lines or number < 1:
            return "none (direct)"
        idx = (number - 1) % len(self._lines)
        line = self._lines[idx]
        try:
            p = urlparse(line)
            return f"proxy {idx + 1}: {p.hostname}:{p.port}"
        except Exception:
            return f"proxy {idx + 1}"


def proxies_path() -> Path:
    return Config.BROWSER_DATA_DIR / "proxies.json"
