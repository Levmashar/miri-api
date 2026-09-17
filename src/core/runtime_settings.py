"""Persisted settings that can be changed from the admin panel."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from src.core.config import Config


class RuntimeSettings:
    """Small atomic JSON-backed store for settings that apply without restart."""

    KEYS = ("new_chat_every_request", "temporary_chats")

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (Config.BROWSER_DATA_DIR / "settings.json")
        self._lock = asyncio.Lock()
        self._values = {
            "new_chat_every_request": bool(Config.NEW_CHAT_EVERY_REQUEST),
            "temporary_chats": bool(Config.TEMPORARY_CHATS),
        }

    async def load(self) -> None:
        """Load persisted overrides, retaining env defaults for missing values."""
        async with self._lock:
            if not self._path.exists():
                self._apply_unlocked()
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("settings file must contain an object")
                for key in self.KEYS:
                    if isinstance(raw.get(key), bool):
                        self._values[key] = raw[key]
                self._apply_unlocked()
            except Exception:
                # A malformed settings file must not prevent the gateway from
                # starting; env defaults remain active and the next admin save
                # repairs the file atomically.
                self._apply_unlocked()

    def snapshot(self) -> dict[str, bool]:
        return dict(self._values)

    async def update(self, changes: dict) -> dict[str, bool]:
        """Validate, persist, and apply the supplied setting changes."""
        unknown = set(changes) - set(self.KEYS)
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        if any(not isinstance(value, bool) for value in changes.values()):
            raise ValueError("settings must be boolean")

        async with self._lock:
            next_values = {**self._values, **changes}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(next_values, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
            self._values = next_values
            self._apply_unlocked()
            return self.snapshot()

    def _apply_unlocked(self) -> None:
        Config.NEW_CHAT_EVERY_REQUEST = self._values["new_chat_every_request"]
        Config.TEMPORARY_CHATS = self._values["temporary_chats"]


def settings_path() -> Path:
    return Config.BROWSER_DATA_DIR / "settings.json"
