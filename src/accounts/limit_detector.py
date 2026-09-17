"""
Limit detection — parse a ChatGPT reply for "you've hit the limit" signals.

HONEST SCOPE: the web UI exposes NO quota API and NO "messages remaining" number.
The only signal is the natural-language banner ChatGPT returns when you're
throttled, e.g. (observed verbatim in this project):

  "You've hit the Free plan limit for image generations requests. You can create
   more images when the limit resets in 23 hours and 24 minutes."

So this is a best-effort regex over scraped text. It WILL rot when ChatGPT
changes wording. A limit we miss just means one failed request before the soft
cap or the next attempt catches it; a false positive puts an account on an
unnecessary cooldown. Kept deliberately conservative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Phrases that mean "throttled". Must co-occur with a limit word to fire.
_LIMIT_PHRASES = [
    r"hit the .{0,40}?limit",
    r"reached .{0,40}?limit",
    r"limit for .{0,40}? requests",
    r"you've reached your .{0,40}?limit",
    r"usage limit",
    r"rate limit",
    r"come back later",
    r"try again later",
    r"upgrade to .{0,30}? for more",
]
_LIMIT_RE = re.compile("|".join(_LIMIT_PHRASES), re.IGNORECASE)

# "resets in 23 hours and 24 minutes" / "in 3h 10m" / "in 45 minutes"
_RESET_RE = re.compile(
    r"resets?\s+(?:in\s+)?"
    r"(?:(?P<h>\d+)\s*(?:hours?|hrs?|h))?[\s,and]*"
    r"(?:(?P<m>\d+)\s*(?:minutes?|mins?|m))?",
    re.IGNORECASE,
)


@dataclass
class LimitInfo:
    hit: bool
    kind: str = ""          # "image" | "message" | "generic"
    reset_seconds: float = 0.0   # seconds until reset, 0 if unknown
    raw: str = ""


def detect_limit(text: str) -> LimitInfo:
    """Inspect an assistant reply for a throttle message."""
    if not text:
        return LimitInfo(hit=False)
    if not _LIMIT_RE.search(text):
        return LimitInfo(hit=False)

    low = text.lower()
    if "image" in low:
        kind = "image"
    elif "message" in low or "gpt" in low:
        kind = "message"
    else:
        kind = "generic"

    reset_seconds = 0.0
    m = _RESET_RE.search(text)
    if m and (m.group("h") or m.group("m")):
        hours = int(m.group("h") or 0)
        mins = int(m.group("m") or 0)
        reset_seconds = hours * 3600 + mins * 60

    return LimitInfo(hit=True, kind=kind, reset_seconds=reset_seconds, raw=text[:300])
