"""
Grok response-completion detector and extractor.

Like Gemini (and unlike ChatGPT) the answer is read straight from the DOM, so
Grok never touches the shared X11 clipboard and never contends on the
process-global clipboard lock.

Assistant turns are identified NEGATIVELY: a `.message-bubble` that is not
inside a `[data-testid="user-message"]`. See selectors.py for why.
"""

from __future__ import annotations

import asyncio
import time

from patchright.async_api import Page

from src.core.log import setup_logging
from src.providers.grok.selectors import GrokSelectors as S

log = setup_logging("grok_detector")

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

# Counts / reads assistant bubbles without depending on an assistant testid.
_JS_ASSISTANT = """
() => {
    const all = Array.from(document.querySelectorAll('.message-bubble'));
    return all.filter(el => !el.closest('[data-testid="user-message"]'));
}
"""


async def count_assistant_turns(page: Page) -> int:
    try:
        return int(await page.evaluate("(" + _JS_ASSISTANT + ")().length") or 0)
    except Exception:
        return 0


async def latest_response_text(page: Page) -> str:
    try:
        return await page.evaluate(
            """
            () => {
                const all = Array.from(document.querySelectorAll('.message-bubble'))
                    .filter(el => !el.closest('[data-testid="user-message"]'));
                if (!all.length) return '';
                return (all[all.length - 1].innerText || '').trim();
            }
            """
        ) or ""
    except Exception:
        return ""


async def has_generated_image(page: Page) -> bool:
    return bool((await latest_text_and_image(page))[1])


async def latest_text_and_image(page: Page) -> "tuple[str, str]":
    """(text, image_src) of the latest assistant turn in ONE evaluate.

    Used only on image requests, where the poll loop needs both each tick — this
    avoids rebuilding the same .message-bubble scan twice per poll."""
    try:
        r = await page.evaluate(
            """
            () => {
                const all = Array.from(document.querySelectorAll('.message-bubble'))
                    .filter(el => !el.closest('[data-testid="user-message"]'));
                if (!all.length) return { text: '', image: '' };
                const last = all[all.length - 1];
                const text = (last.innerText || '').trim();
                let image = '';
                last.querySelectorAll('img').forEach(img => {
                    const src = img.src || '';
                    if (img.complete && img.naturalWidth >= 200 && img.naturalHeight >= 200 &&
                        (src.startsWith('http') || src.startsWith('blob:'))) image = src;
                });
                return { text, image };
            }
            """
        )
        return (r or {}).get("text", ""), (r or {}).get("image", "")
    except Exception:
        return "", ""


async def wait_for_response_complete(
    page: Page,
    *,
    pre_count: int,
    timeout_ms: int,
    expect_image: bool = False,
    poll_ms: int = 400,
    stable_polls: int = 4,
) -> bool:
    """Wait for a NEW assistant turn that has stopped changing.

    One shared deadline for the whole wait — no strategy restarts the clock.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    log.info(f"Waiting for Grok response (timeout {timeout_ms}ms, pre_count={pre_count})")

    appeared = False
    while time.monotonic() < deadline:
        if await count_assistant_turns(page) > pre_count:
            appeared = True
            break
        await asyncio.sleep(poll_ms / 1000)
    if not appeared:
        log.warning("No new Grok assistant turn appeared before timeout")
        return False

    last_fp = None
    stable = 0
    while time.monotonic() < deadline:
        text, img_src = await latest_text_and_image(page)
        streaming = await page.evaluate(_JS_STOPPING, list(S.STOP_BUTTON))
        if streaming or (expect_image and not img_src) or await count_assistant_turns(page) <= pre_count:
            stable = 0
            last_fp = None
            await asyncio.sleep(poll_ms / 1000)
            continue
        fp = (text, img_src)
        if (text or img_src) and fp == last_fp:
            stable += 1
            if stable >= stable_polls:
                log.info(f"Grok response settled ({len(text)} chars)")
                return True
        else:
            stable = 0
            last_fp = fp
        await asyncio.sleep(poll_ms / 1000)

    log.warning("Grok response never stabilised before timeout")
    return False


async def extract_latest_response(page: Page) -> str:
    text = await latest_response_text(page)
    if text:
        log.info(f"Extracted Grok response via DOM ({len(text)} chars)")
    else:
        log.warning("Grok response extraction returned empty text")
    return text
