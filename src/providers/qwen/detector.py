"""
Qwen response-completion detectors — for chat text AND generated media.

Chat: a thin binding of the shared DOM detector (src/providers/dom_detector.py)
to Qwen's selectors — a new assistant turn must appear, then its text must stop
changing. Read straight from the DOM, so Qwen tabs never contend on the X11
clipboard lock.

Media: current DOM results plus this prompt's observed completion/task results.
Snapshot every image/video src on the page just before sending, then poll for a
src that was not in that snapshot, is big enough to be a result, and is not the
user's own upload or the composer preview. Polling ends early on a failure or
usage-limit message in the response.
"""

from __future__ import annotations

import asyncio
import re
import time

from src.accounts.limit_detector import detect_limit
from src.core.log import setup_logging
from src.providers import dom_detector
from src.providers.qwen.selectors import QwenSelectors as S

log = setup_logging("qwen_detector")

NAME = "Qwen"

_FAILURE = re.compile(S.MEDIA_FAILURE_TEXT_RE, re.IGNORECASE)


# ── Chat ────────────────────────────────────────────────────────

async def count_assistant_turns(page) -> int:
    return await dom_detector.count_assistant_turns(page, S.ASSISTANT_TURN, S.USER_TURN)


async def latest_response_text(page) -> str:
    return await dom_detector.latest_response_text(page, S.ASSISTANT_TURN, S.USER_TURN)


async def wait_for_response_complete(page, *, pre_count: int, timeout_ms: int,
                                     **kwargs) -> bool:
    return await dom_detector.wait_for_response_complete(
        page,
        turn_selectors=S.ASSISTANT_TURN,
        user_selector=S.USER_TURN,
        pre_count=pre_count,
        timeout_ms=timeout_ms,
        log=log,
        name=NAME,
        stop_selectors=S.STOP_BUTTON,
        **kwargs,
    )


async def extract_latest_response(page) -> str:
    return await dom_detector.extract_latest_response(
        page, S.ASSISTANT_TURN, S.USER_TURN, log=log, name=NAME
    )


# ── Media ───────────────────────────────────────────────────────

# Every candidate result currently on the page: [{src, ready}] for the requested
# kind. `ready` means "finished enough to download" — a loaded, large image, or
# a video with a real (http/blob) source. De-duplicated by src across scopes.
_JS_MEDIA = """
(args) => {
    const seen = new Set();
    const out = [];
    const ok = (src) => src && (src.startsWith('http') || src.startsWith('blob:'));
    for (const scopeSel of args.scopes) {
        let scopes = [];
        try { scopes = Array.from(document.querySelectorAll(scopeSel)); }
        catch (e) { continue; }
        for (const scope of scopes) {
            if (args.kind === 'video') {
                scope.querySelectorAll('video').forEach((v) => {
                    if (args.exclude && v.closest(args.exclude)) return;
                    const inner = v.querySelector('source');
                    const src = v.getAttribute('src') || (inner ? inner.src : '') || v.currentSrc || '';
                    if (!ok(src) || seen.has(src)) return;
                    seen.add(src);
                    const card = v.closest('.qwen-video');
                    out.push({ src, ready: !card || !card.querySelector('.qwen-video-generating, .qwen-video-error') });
                });
            } else {
                scope.querySelectorAll('img').forEach((img) => {
                    if (args.exclude && img.closest(args.exclude)) return;
                    if (img.closest('.qwen-video, .qwen-video-player') || img.matches('.video-cover')) return;
                    const src = img.src || img.currentSrc || '';
                    if (!ok(src) || seen.has(src)) return;
                    seen.add(src);
                    const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
                    const card = img.closest('.qwen-image');
                    out.push({
                        src,
                        ready: !(card && card.querySelector('.qwen-image-generating')) &&
                            !!img.complete && (card ? w > 0 && h > 0 : w >= args.minDim && h >= args.minDim),
                    });
                });
            }
        }
    }
    return out;
}
"""


def _args(kind: str) -> dict:
    return {
        "scopes": list(S.MEDIA_SCOPE),
        "exclude": S.MEDIA_EXCLUDE,
        "kind": kind,
        "minDim": S.MIN_MEDIA_DIM,
    }


async def _media(page, kind: str) -> list:
    try:
        return await page.evaluate(_JS_MEDIA, _args(kind)) or []
    except Exception:
        return []


async def capture_media_srcs(page, kind: str) -> set:
    """Baseline: every image/video src already on the page, ready or not.

    Taken immediately before sending, AFTER any reference upload — so the
    composer's preview of an uploaded image is part of the baseline and can
    never be mistaken for the result.
    """
    return {m.get("src", "") for m in await _media(page, kind) if m.get("src")}


async def wait_for_new_media(
    page,
    *,
    kind: str,
    baseline: set,
    timeout_ms: int,
    settle_s: float = 6.0,
    poll_ms: int = 2000,
    capture=None,
) -> list:
    """Poll until NEW finished media appears; return its srcs ([] on timeout).

    After the first result, keep collecting for `settle_s` — a request can
    produce several images, and they rarely finish in the same poll.

    Raises RuntimeError("limit: ...") on a usage-limit message and
    RuntimeError("failed: ...") when the response says generation failed.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    found = []
    previous = set()
    settled_at = time.monotonic()
    log.info(f"Qwen: waiting for {kind} (timeout {timeout_ms}ms, {len(baseline)} baseline)")

    candidates = []
    while time.monotonic() < deadline:
        candidates = await _media(page, kind)
        found = []
        if capture:
            found.extend(src for src in capture.urls if src not in baseline)
        for m in candidates:
            src = m.get("src", "")
            if src and src not in baseline and m.get("ready"):
                if src not in found:
                    found.append(src)

        if found:
            now = time.monotonic()
            if set(found) != previous:
                settled_at = now
            elif now - settled_at >= settle_s:
                break
        else:
            if capture and capture.error:
                raise RuntimeError(capture.error)
            # Only read the reply for bad news while nothing has been produced:
            # once media exists, words like "failed" in the caption are noise.
            text = await latest_response_text(page)
            if text:
                limit = detect_limit(text)
                if limit.hit:
                    raise RuntimeError(f"limit: {text[:300]}")
                if _FAILURE.search(text):
                    raise RuntimeError(f"failed: {text[:300]}")

        previous = set(found)
        await asyncio.sleep(min(poll_ms / 1000, max(0, deadline - time.monotonic())))

    srcs = list(found)
    if srcs:
        log.info(f"Qwen: {len(srcs)} new {kind}(s) ready")
    else:
        log.warning('Qwen: no finished %s before timeout; candidates=%s pending=%s network=%s',
                    kind, len(candidates), sum(not m.get('ready') for m in candidates),
                    capture.diagnostics() if capture else {})
    return srcs
