"""
Process-wide lock for the OS clipboard.

Response extraction copies the assistant's message via the page's "copy" button
and reads it back through navigator.clipboard (see the detectors'
extract_last_response_via_copy). In headed Chrome under Xvfb that is the single
shared X11 clipboard — every tab writes to and reads from the same one.

With MAX_CONCURRENT_REQUESTS > 1 there are multiple tabs. If two of them run the
copy→read step at the same time, tab B can read tab A's copied text: the exact
cross-request mix-up we must never allow. This lock makes that step mutually
exclusive across all tabs and both providers (ChatGPT and Claude share the same
clipboard, so they share this lock).

Only the clipboard critical section is guarded — a few hundred milliseconds —
not the slow generation wait, so requests still overlap for almost their entire
duration.

Lazily constructed: on Python 3.9 (the Dockerfile's runtime) asyncio.Lock binds
the event loop at construction, so a module-level Lock() would bind the wrong
loop and raise on first contention. See src/api/browser_lock.py for the same
pattern and reasoning.
"""

from __future__ import annotations

import asyncio

_clipboard_lock: asyncio.Lock | None = None


def get_clipboard_lock() -> asyncio.Lock:
    """Get (or lazily create) the shared clipboard lock."""
    global _clipboard_lock
    if _clipboard_lock is None:
        _clipboard_lock = asyncio.Lock()
    return _clipboard_lock
