"""Select Qwen's media tool and require visible confirmation before sending."""

import asyncio
import time

from src.providers.composer import _JS_HELPERS, _JS_BUTTON
from src.providers.qwen.selectors import QwenSelectors as S
from src.core.log import setup_logging

log = setup_logging('qwen_media_mode')


_JS_TOOL = "args => {" + _JS_HELPERS + r"""
    const normal = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const labels = args.labels.map(normal);
    const matches = n => {
        const title = n.querySelector('.mode-select-dropdown-item-name') ||
            (n.matches('.mode-select-current-mode') ? n : null);
        const copy = title && title.cloneNode(true);
        if (copy) copy.querySelectorAll('.mode-select-dropdown-item-tag, .mode-select-current-mode-close, .mode-select-current-mode-icon').forEach(e => e.remove());
        const texts = [n.getAttribute('aria-label'), n.getAttribute('title'),
                       copy && copy.textContent,
                       (n.innerText || '').split('\n')[0]].map(normal);
        return texts.some(t => labels.includes(t));
    };
    const state = e => {
        for (let n = e, i = 0; n && i < 3; n = n.parentElement, i++) {
            for (const a of ['aria-pressed', 'aria-checked', 'aria-selected']) {
                if (n.getAttribute(a) === 'true') return true;
                if (n.getAttribute(a) === 'false') return false;
            }
            const ds = n.getAttribute('data-state');
            if (['active', 'on', 'checked', 'selected'].includes(ds)) return true;
            if (['inactive', 'off', 'unchecked'].includes(ds)) return false;
            if (/(^|[\s_-])(active|selected|checked)([\s_-]|$)/i.test(n.className || '')) return true;
        }
        return null;
    };
    const candidates = nodes(args.sels).filter(e => visible(e) && !disabled(e) &&
        !e.closest('[class*="item-disabled"], [class*="mode-disabled"]') &&
        !e.closest('nav, aside, [class*="user-message"], [class*="response-message"]') && matches(e));
    const selected = candidates.find(e => state(e) === true ||
        (e.matches('.mode-select-current-mode') && !!e.querySelector('.mode-select-current-mode-close')) ||
        !!e.querySelector('.mode-selector-drawer-list-item-check, .mode-select-dropdown-item-icon-checked') ||
        (!e.closest('[role="menu"], [role="listbox"]') &&
         /chip|tag/i.test(e.className || '') &&
         !!e.querySelector('[aria-label*="remove" i], [aria-label*="close" i], [class*="close"]')));
    if (selected) return {selected: true, found: true};
    if (!args.click || !candidates.length) return {selected: false, found: !!candidates.length};
    for (const e of candidates) {
        e.scrollIntoView({block: 'nearest'});
        const r = e.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
        const top = document.elementFromPoint(x, y);
        if (!top || (top !== e && !e.contains(top))) continue;
        return {selected: false, found: true, x, y};
    }
    return {selected: false, found: true};
} """


async def select_media_tool(page, labels, *, timeout_ms=12000):
    args = {'sels': S.MEDIA_TOOL, 'labels': list(labels), 'click': False}
    if (await page.evaluate(_JS_TOOL, args))['selected']:
        return True
    # Wait through hydration, open the mode menu, click one tool, then verify
    # the actual current-mode control. Finding a menu option alone isn't proof.
    deadline = time.monotonic() + timeout_ms / 1000
    opened_at = 0
    opens = 0
    clicked = False
    while time.monotonic() < deadline:
        result = await page.evaluate(_JS_TOOL, {**args, 'click': True})
        if result.get('selected'):
            await page.keyboard.press('Escape')
            return True
        if not clicked and 'x' in result:
            await page.mouse.click(result['x'], result['y'])
            clicked = True
        elif not clicked and not result['found'] and opens < 2 and time.monotonic() - opened_at > 1:
            # A pre-hydration opener can ignore the first click. Retry only
            # the opener, never a dispatched tool toggle or generation.
            menu_open = await page.locator('.mode-select-dropdown-menu:visible, .mode-selector-drawer-list-item:visible, [role="menu"]:visible').count()
            if not menu_open:
                opener = await page.evaluate(_JS_BUTTON, S.MODE_MENU_BUTTON)
                if 'x' not in opener:
                    opener = await page.evaluate(_JS_TOOL, {
                        'sels': ['button', '[role="button"]'],
                        'labels': ['Select Mode', 'Tools', 'More', '+'], 'click': True,
                    })
                if 'x' in opener:
                    await page.mouse.click(opener['x'], opener['y'])
                    opened_at = time.monotonic()
                    opens += 1
        await asyncio.sleep(.1)
    # Only mode-control labels, never composer text, conversation or account data.
    available = await page.locator('.mode-select-dropdown-item-name, .mode-select-current-mode').all_text_contents()
    log.warning('Qwen tool selection failed: requested=%s opened=%s clicked=%s controls=%s',
                labels[0], opens, clicked, [s[:80] for s in available[:16]])
    await page.keyboard.press('Escape')
    return False


async def clear_media_tool(page, *, timeout_ms=3000):
    """Use Qwen's explicit remove control, shared by image and video modes."""
    if not await page.locator('.mode-select-current-mode:visible').count():
        return
    button = await page.evaluate(_JS_BUTTON, S.MEDIA_MODE_CLOSE)
    if 'x' not in button:
        raise RuntimeError('Qwen: current tool is locked; could not return to text chat')
    await page.mouse.click(button['x'], button['y'])
    await page.locator('.mode-select-current-mode:visible').wait_for(state='hidden', timeout=timeout_ms)
