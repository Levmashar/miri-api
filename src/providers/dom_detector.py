"""
Generic DOM response detector for text-only chat providers.

The ChatGPT/Claude detectors read the answer off the OS clipboard (a copy-button
click), which forces every tab in the process to queue on the shared X11
clipboard lock. The Gemini and Grok detectors instead read the answer straight
out of the DOM and never take that lock. This module is that same DOM strategy,
parameterised by selectors so a provider only has to say WHICH nodes are its
assistant turns — DeepSeek and Qwen both use it.

The completion rule is the one that has proven reliable across the providers
here:
 1. wait for a NEW assistant turn to appear (count grows past the pre-send count)
 2. wait for its text to stop changing (stable across N consecutive polls)
One shared deadline covers the whole wait — no strategy restarts the clock.

Assistant turns can be identified positively (a selector that matches only the
model's turns) or negatively (any message node NOT inside a user turn). Both
are supported: pass `turn_selectors` and optionally `user_selector`, and the
first selector that matches anything wins.
"""

from __future__ import annotations

import asyncio
import time

# Returns the assistant-turn nodes for the given selectors. The first selector
# that matches ANY node wins, so a provider can list a precise selector first
# and looser fallbacks after it without the fallbacks polluting the result.
_JS_TURNS = """
(args) => {
    const pick = () => {
        for (const sel of args.sels) {
            let nodes = [];
            try { nodes = Array.from(document.querySelectorAll(sel)); }
            catch (e) { continue; }
            if (args.userSel) {
                nodes = nodes.filter((n) => !n.closest(args.userSel));
            }
            if (nodes.length) return nodes;
        }
        return [];
    };
    return pick();
}
"""

_JS_COUNT = "(args) => (" + _JS_TURNS + ")(args).length"

_JS_LAST_TEXT = """
(args) => {
    const turns = (""" + _JS_TURNS + """)(args);
    if (!turns.length) return '';
    return (turns[turns.length - 1].innerText || '').trim();
}
"""

_JS_STREAMING = """
(sels) => sels.some(sel => {
    try {
        return Array.from(document.querySelectorAll(sel)).some(n =>
            !n.disabled && n.getAttribute('aria-disabled') !== 'true' &&
            (n.offsetParent !== null || n.getClientRects().length > 0));
    } catch (e) { return false; }
})
"""


def _args(turn_selectors, user_selector: str = "") -> dict:
    return {"sels": list(turn_selectors), "userSel": user_selector or ""}


async def count_assistant_turns(page, turn_selectors, user_selector: str = "") -> int:
    try:
        return int(await page.evaluate(_JS_COUNT, _args(turn_selectors, user_selector)) or 0)
    except Exception:
        return 0


async def latest_response_text(page, turn_selectors, user_selector: str = "") -> str:
    try:
        return await page.evaluate(
            _JS_LAST_TEXT, _args(turn_selectors, user_selector)
        ) or ""
    except Exception:
        return ""


async def wait_for_response_complete(
    page,
    *,
    turn_selectors,
    user_selector: str = "",
    pre_count: int,
    timeout_ms: int,
    log,
    name: str = "provider",
    poll_ms: int = 400,
    stable_polls: int = 4,
    stop_selectors=(),
) -> bool:
    """Wait until a NEW assistant turn has appeared and stopped changing.

    Returns True on a settled response, False on timeout. Callers must not
    extract the previous response after a timeout with no new assistant turn.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    log.info(f"Waiting for {name} response (timeout {timeout_ms}ms, pre_count={pre_count})")

    appeared = False
    while time.monotonic() < deadline:
        if await count_assistant_turns(page, turn_selectors, user_selector) > pre_count:
            appeared = True
            break
        await asyncio.sleep(poll_ms / 1000)
    if not appeared:
        log.warning(f"No new {name} assistant turn appeared before timeout")
        return False

    last_text = None
    stable = 0
    while time.monotonic() < deadline:
        text = await latest_response_text(page, turn_selectors, user_selector)
        streaming = bool(await page.evaluate(_JS_STREAMING, list(stop_selectors))) if stop_selectors else False
        if text and text == last_text and not streaming:
            stable += 1
            if stable >= stable_polls:
                log.info(f"{name} response settled ({len(text)} chars)")
                return True
        else:
            stable = 0
            last_text = text
        await asyncio.sleep(poll_ms / 1000)

    log.warning(f"{name} response never stabilised before timeout")
    return False


async def extract_latest_response(page, turn_selectors, user_selector: str = "",
                                  *, log, name: str = "provider") -> str:
    """The assistant's answer text. DOM-based — never touches the clipboard."""
    text = await latest_response_text(page, turn_selectors, user_selector)
    if text:
        log.info(f"Extracted {name} response via DOM ({len(text)} chars)")
    else:
        log.warning(f"{name} response extraction returned empty text")
    return text
