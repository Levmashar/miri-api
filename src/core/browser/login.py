"""Read-only, bounded login checks that tolerate restored SPA sessions."""

import asyncio
import time
from urllib.parse import urlsplit


_VISIBLE = """elements => elements.some(e => {
    if (e.closest('[data-message-author-role], [class*="user-message"], [class*="response-message"]')) return false;
    if (!e.getClientRects().length) return false;
    for (let n=e; n; n=n.parentElement) {
        const s=getComputedStyle(n);
        if (n.hidden || n.getAttribute('aria-hidden')==='true' || s.display==='none' ||
            s.visibility==='hidden' || s.opacity==='0') return false;
    }
    return true;
})"""


async def _visible(page, selectors):
    if not selectors:
        return False
    # Playwright selectors support :has-text; evaluate every match, including
    # the visible login button after a hidden mobile/desktop duplicate.
    return await page.locator(', '.join(selectors)).evaluate_all(_VISIBLE)


async def page_logged_in(page, spec):
    if page.is_closed() or urlsplit(page.url).hostname != urlsplit(spec.url).hostname:
        return False
    if await _visible(page, spec.login_indicators):
        return False
    positive = await _visible(page, spec.logged_in_indicators)
    if not positive and not spec.login_requires_account_marker:
        positive = await _visible(page, spec.chat_input)
    # The page can finish loading its anonymous header during the awaits above.
    return positive and not await _visible(page, spec.login_indicators)


async def wait_for_login(pages, spec, *, timeout_ms=8000):
    """Require the same tab to look signed in across consecutive observations.

    `pages` is a callable so a noVNC login in a new tab is discovered. Nothing
    navigates or reloads a tab. An expired session never becomes schedulable.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    positive_since = {}
    while True:
        now = time.monotonic()
        candidates = list(pages())
        results = await asyncio.gather(*(page_logged_in(p, spec) for p in candidates), return_exceptions=True)
        current = {}
        for page, result in zip(candidates, results):
            if result is True:
                since = positive_since.get(page, now)
                if now - since >= .4:
                    return True
                current[page] = since
        positive_since = current
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(.2, max(0, deadline - time.monotonic())))
