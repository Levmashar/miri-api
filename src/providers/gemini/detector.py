"""
Gemini response-completion detector and extractor.

Unlike ChatGPT (where we click a copy button and read the OS clipboard), Gemini
exposes the answer directly as text in <message-content>. That is a real win for
concurrency: Gemini extraction never touches the shared X11 clipboard, so Gemini
tabs never contend on the process-global clipboard lock.

Completion strategy:
 1. Wait for a NEW <model-response> to appear (count grows past the pre-send count).
 2. Wait for its text to STOP changing (stable across N consecutive polls), which
    is what "streaming finished" looks like from the DOM.
 3. Belt-and-braces: if a stop button is present, treat its disappearance as done.
"""

from __future__ import annotations

import asyncio
import time

from patchright.async_api import Page

from src.providers.gemini.selectors import GeminiSelectors as S
from src.core.log import setup_logging

log = setup_logging("gemini_detector")

_JS_STOPPING = """
sels => sels.some(sel => Array.from(document.querySelectorAll(sel)).some(e => {
    if (e.disabled || e.getAttribute('aria-disabled') === 'true' || !e.getClientRects().length) return false;
    for (let n = e; n; n = n.parentElement) {
        const style = getComputedStyle(n);
        if (n.hidden || n.getAttribute('aria-hidden') === 'true' ||
            style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    }
    return true;
}))
"""


async def count_model_responses(page: Page) -> int:
    """How many assistant turns are currently rendered."""
    try:
        return int(await page.evaluate(
            f"document.querySelectorAll('{S.MODEL_TURN}').length"
        ) or 0)
    except Exception:
        return 0


async def latest_response_text(page: Page) -> str:
    """innerText of the LAST assistant turn (empty string if none)."""
    try:
        return await page.evaluate(
            """
            () => {
                const turns = document.querySelectorAll('model-response');
                if (!turns.length) return '';
                const last = turns[turns.length - 1];
                const mc = last.querySelector('message-content') || last;
                return (mc.innerText || '').trim();
            }
            """
        ) or ""
    except Exception:
        return ""


# A generated image is a big raster served from Google user content, and
# crucially it may sit inside various wrappers (single-image, generated-image,
# image-panel, ...), so we DON'T require it be nested in <message-content> — just
# in the newest assistant turn, and not a tiny avatar/icon.
_JS_LATEST_IMAGE = """
() => {
    const turns = document.querySelectorAll('model-response');
    if (!turns.length) return '';
    const last = turns[turns.length - 1];
    let best = '';
    last.querySelectorAll('img').forEach(img => {
        const w = img.naturalWidth || 0;
        const h = img.naturalHeight || 0;
        const src = img.src || '';
        // Gemini renders generated images as blob: URLs, not http.
        if (!src.startsWith('http') && !src.startsWith('blob:')) return;
        if (img.complete && w >= 240 && h >= 240) best = src;
    });
    return best;
}
"""


async def latest_image_src(page: Page) -> str:
    """src of the generated image in the latest assistant turn, or ''."""
    try:
        return await page.evaluate(_JS_LATEST_IMAGE) or ""
    except Exception:
        return ""


async def has_generated_image(page: Page) -> bool:
    return bool(await latest_image_src(page))


# Fused text+image read: the stability poll needs BOTH every tick, so scan the
# latest <model-response> once per tick instead of two separate CDP round-trips.
_JS_TEXT_AND_IMAGE = """
() => {
    const turns = document.querySelectorAll('model-response');
    if (!turns.length) return {text: '', img: ''};
    const last = turns[turns.length - 1];
    const mc = last.querySelector('message-content') || last;
    const text = (mc.innerText || '').trim();
    let best = '';
    last.querySelectorAll('img').forEach(img => {
        const w = img.naturalWidth || 0;
        const h = img.naturalHeight || 0;
        const src = img.src || '';
        if (!src.startsWith('http') && !src.startsWith('blob:')) return;
        if (img.complete && w >= 240 && h >= 240) best = src;
    });
    return {text: text, img: best};
}
"""


async def latest_text_and_image(page: Page) -> "tuple[str, str]":
    """(text, image_src) of the latest assistant turn in ONE evaluate."""
    try:
        r = await page.evaluate(_JS_TEXT_AND_IMAGE) or {}
        return (r.get("text") or "", r.get("img") or "")
    except Exception:
        return ("", "")


async def wait_for_response_complete(
    page: Page,
    *,
    pre_count: int,
    timeout_ms: int,
    expect_image: bool = False,
    poll_ms: int = 400,
    stable_polls: int = 4,
) -> bool:
    """Wait until a NEW assistant turn has appeared and stopped changing.

    Returns True on a settled response, False on timeout. One shared deadline
    covers the whole wait — no strategy gets to restart the clock (that bug cost
    this project 10-minute hangs on the ChatGPT side).
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    log.info(f"Waiting for Gemini response (timeout {timeout_ms}ms, pre_count={pre_count})")

    # ── 1. a new turn must appear ──
    appeared = False
    while time.monotonic() < deadline:
        if await count_model_responses(page) > pre_count:
            appeared = True
            break
        await asyncio.sleep(poll_ms / 1000)
    if not appeared:
        log.warning("No new Gemini response turn appeared before timeout")
        return False

    # ── 2. its text must stabilise (streaming finished) ──
    # Stability = the response stopped changing. For an image request the answer
    # may be an IMAGE with little/no text, so we track a fingerprint of both text
    # AND the image src and settle when EITHER has content and is unchanged.
    last_fp = None
    stable = 0
    while time.monotonic() < deadline:
        text, img_src = await latest_text_and_image(page)
        has_img = bool(img_src)
        streaming = await page.evaluate(_JS_STOPPING, list(S.STOP_BUTTON))

        # If this was an image request, only settle once an image is actually
        # present (Gemini streams text like "Sure, here's..." well before the
        # image finishes rendering).
        if streaming or (expect_image and not has_img) or await count_model_responses(page) <= pre_count:
            stable = 0
            last_fp = ("", "")
            await asyncio.sleep(poll_ms / 1000)
            continue

        fp = (text, img_src)
        if (text or has_img) and fp == last_fp:
            stable += 1
            if stable >= stable_polls:
                log.info(f"Gemini response settled ({len(text)} chars, image={has_img})")
                return True
        else:
            stable = 0
            last_fp = fp
        await asyncio.sleep(poll_ms / 1000)

    log.warning("Gemini response never stabilised before timeout")
    return False


async def extract_latest_response(page: Page) -> str:
    """The assistant's answer text. DOM-based — never touches the clipboard."""
    text = await latest_response_text(page)
    if text:
        log.info(f"Extracted Gemini response via DOM ({len(text)} chars)")
    else:
        log.warning("Gemini response extraction returned empty text")
    return text
