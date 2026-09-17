"""Insert a complete draft and confirm the web UI accepted one submission."""

import asyncio
import time

from src.core.log import setup_logging

log = setup_logging("composer")


_JS_HELPERS = r"""
    const nodes = sels => {
        const out = new Set();
        for (const s of sels) {
            try { document.querySelectorAll(s).forEach(e => out.add(e)); } catch (_) {}
        }
        return [...out];
    };
    const visible = e => {
        if (!e || !e.getClientRects().length) return false;
        for (let n = e; n; n = n.parentElement) {
            const style = getComputedStyle(n);
            if (n.hidden || n.getAttribute('aria-hidden') === 'true' ||
                style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') return false;
        }
        return true;
    };
    const disabled = e => e.matches(':disabled') || e.readOnly ||
        !!e.closest('[aria-disabled="true"], [inert]') ||
        /(^|[\s_-])disabled([\s_-]|$)/i.test(e.className || '');
    const editors = sels => nodes(sels).filter(e => visible(e) &&
        (e instanceof HTMLTextAreaElement || e.isContentEditable));
    const copiedValue = e => {
        const selection = window.getSelection(), previous = [];
        for (let i = 0; i < selection.rangeCount; i++) previous.push(selection.getRangeAt(i).cloneRange());
        const range = document.createRange(); range.selectNodeContents(e);
        selection.removeAllRanges(); selection.addRange(range);
        const text = selection.toString();
        selection.removeAllRanges(); previous.forEach(r => selection.addRange(r));
        return text;
    };
    const domValue = e => {
        // ProseMirror stores pasted lines as <p>/<div>/<br> nodes. textContent
        // drops their boundaries and innerText may double paragraph breaks, so
        // rebuild the plain-text value the editor model represents.
        const blocks = new Set(['ADDRESS', 'ARTICLE', 'BLOCKQUOTE', 'DIV', 'FIGCAPTION',
            'FOOTER', 'HEADER', 'LI', 'MAIN', 'NAV', 'P', 'PRE', 'SECTION']);
        const walk = node => {
            if (node.nodeType === Node.TEXT_NODE) return node.data || '';
            if (node.nodeType !== Node.ELEMENT_NODE) return '';
            if (node.tagName === 'BR') return '\n';
            let text = '';
            node.childNodes.forEach(child => { text += walk(child); });
            if (blocks.has(node.tagName) && !text.endsWith('\n')) text += '\n';
            return text;
        };
        let text = '';
        e.childNodes.forEach(child => { text += walk(child); });
        return text.endsWith('\n') ? text.slice(0, -1) : text;
    };
    const norm = s => (s == null ? '' : String(s)).replace(/\r\n?/g, '\n');
    // Rich composers rewrite whitespace: ProseMirror stores a run of spaces as
    // NBSP, adds a trailing blank paragraph, and drops the space before a line
    // break. None of that loses a single character of the prompt, so comparing
    // raw strings rejects drafts that are in fact intact. canon() folds exactly
    // those rewrites away; squeeze() is the last resort — same characters,
    // whitespace ignored entirely — which still catches real truncation.
    const canon = s => norm(s)
        .replace(/[   -   　]/g, ' ')
        .replace(/[​-‍⁠﻿]/g, '')
        .split('\n').map(line => line.replace(/[ \t]+$/, '')).join('\n')
        .replace(/^\n+|\n+$/g, '');
    const squeeze = s => canon(s).replace(/\s+/g, '');
    // Where two readings first diverge, in code points — the only detail that
    // makes a rejected send diagnosable after the fact.
    const divergence = (got, want) => {
        let i = 0;
        while (i < got.length && i < want.length && got[i] === want[i]) i++;
        const points = t => [...t].slice(0, 12).map(c => c.codePointAt(0));
        return {at: i, got: points(got.slice(i)), want: points(want.slice(i)),
                gotLen: got.length, wantLen: want.length};
    };
    const values = e => {
        if (e instanceof HTMLTextAreaElement) return [norm(e.value)];
        const readings = [copiedValue(e), e.innerText, domValue(e), e.textContent];
        return [...new Set(readings.map(norm))];
    };
    const activeEditor = list => {
        const active = document.activeElement;
        return list.find(e => e === active || e.contains(active)) || list[0] || null;
    };
"""

_JS_FOCUS = "args => {" + _JS_HELPERS + """
    const e = editors(args.inputs).find(e => !disabled(e));
    if (!e) return false;
    e.focus();
    if (document.activeElement !== e) return false;
    if (args.select) {
        if (e instanceof HTMLTextAreaElement) e.select();
        else {
            const range = document.createRange(); range.selectNodeContents(e);
            const selection = window.getSelection();
            selection.removeAllRanges(); selection.addRange(range);
        }
    }
    return true;
} """

_JS_STATE = "args => {" + _JS_HELPERS + """
    const inputs = editors(args.inputs);
    const input = activeEditor(inputs);
    const drafts = input ? values(input) : [];
    const userTurns = nodes(args.users).filter(visible);
    const wanted = norm(args.text);
    const matches = drafts.some(d => canon(d) === canon(wanted));
    const sameContent = drafts.some(d => squeeze(d) === squeeze(wanted));
    // Closest reading = longest common prefix, so the diagnostic describes the
    // draft that nearly worked rather than an unrelated empty editor.
    const closest = drafts.slice(1).reduce((best, d) =>
        divergence(canon(d), canon(wanted)).at > divergence(canon(best), canon(wanted)).at ? d : best,
        drafts[0] === undefined ? '' : drafts[0]);
    return {
        draft: drafts.length ? drafts[0] : null,
        drafts,
        draftMatches: matches,
        draftSameContent: sameContent,
        draftExact: drafts.includes(wanted),
        mismatch: matches ? null : divergence(canon(closest), canon(wanted)),
        draftEmpty: drafts.includes(''),
        editor: input ? `${input.tagName.toLowerCase()}#${input.id || ''}[contenteditable=${input.getAttribute('contenteditable')}]` : 'missing',
        userMatches: userTurns.filter(e => norm(e.innerText || '').trim() === norm(args.text).trim()).length,
        stopping: nodes(args.stops).some(e => visible(e) && !disabled(e)),
    };
} """

_JS_BUTTON = "sels => {" + _JS_HELPERS + """
    let found = false;
    for (const e of nodes(sels)) {
        if (!visible(e)) continue;
        found = true;
        if (disabled(e)) continue;
        e.scrollIntoView({block: 'nearest', inline: 'nearest'});
        const r = e.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
        const top = document.elementFromPoint(x, y);
        if (top && (top === e || e.contains(top))) return {found: true, x, y};
    }
    return {found};
} """


def _list(selectors):
    return [selectors] if isinstance(selectors, str) else list(selectors)


def _diagnose(state, text, name):
    """Human-readable account of why a draft reading did not match."""
    lengths = sorted({len(draft) for draft in (state or {}).get('drafts', [])})
    expected = len(text.replace('\r\n', '\n').replace('\r', '\n'))
    detail = (f"expected {expected} chars; editor readings {lengths or ['missing']}; "
              f"{(state or {}).get('editor', 'missing')}")
    diff = (state or {}).get('mismatch')
    if diff:
        detail += (f"; diverges at char {diff['at']} of {diff['wantLen']} "
                   f"(got U+{'/U+'.join(f'{c:04X}' for c in diff['got']) or '—'}, "
                   f"want U+{'/U+'.join(f'{c:04X}' for c in diff['want']) or '—'})")
    return detail


async def _insert(page, text, args, *, use_fill, name, timeout_ms):
    """Replace the focused composer's content with `text` in one edit."""
    # Playwright fill speaks the native input/contenteditable protocol and is
    # more reliable for ChatGPT's ProseMirror than a browser Selection followed
    # by insertText. Neither route generates an Enter key event per newline.
    if not use_fill:
        await page.keyboard.insert_text(text)
        return
    marked = await page.evaluate("args => {" + _JS_HELPERS + """
        const e = activeEditor(editors(args.inputs));
        if (!e) return false;
        document.querySelectorAll('[data-miri-active-composer]').forEach(n =>
            n.removeAttribute('data-miri-active-composer'));
        e.setAttribute('data-miri-active-composer', '1');
        return true;
    } """, args)
    if not marked:
        raise RuntimeError(f"{name}: composer lost before text entry")
    try:
        await page.locator('[data-miri-active-composer="1"]').fill(text, timeout=timeout_ms)
    finally:
        try:
            await page.evaluate("document.querySelectorAll('[data-miri-active-composer]').forEach(e => e.removeAttribute('data-miri-active-composer'))")
        except Exception:
            pass


async def enter_prompt(page, text, input_selectors, *, name="provider", timeout_ms=10000,
                       prefer_fill=False, attempts=2):
    """Put the whole prompt in the composer, or raise without sending anything.

    A controlled editor can swallow or mangle an edit — a React re-render lands
    mid-fill, the page steals focus, ProseMirror rebuilds paragraphs late. That
    is transient, so the insert is retried on the OTHER insertion route before
    giving up; the old single-shot version turned each of those into a 500 with
    the prompt left sitting visibly in the chat box.
    """
    args = {'inputs': _list(input_selectors), 'select': True}
    deadline = time.monotonic() + timeout_ms / 1000
    state = None
    for attempt in range(max(1, attempts)):
        while not await page.evaluate(_JS_FOCUS, args):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{name}: could not find an editable chat input")
            await asyncio.sleep(.1)

        await _insert(page, text, args, use_fill=prefer_fill if attempt == 0 else not prefer_fill,
                      name=name, timeout_ms=timeout_ms)

        # Controlled editors may rebuild their paragraph DOM asynchronously.
        verify_deadline = min(deadline, time.monotonic() + 1.5)
        while True:
            state = await page.evaluate(_JS_STATE, {**args, 'text': text, 'users': [], 'stops': []})
            if state['draftMatches'] or state['draftSameContent']:
                if not state['draftMatches']:
                    log.warning("%s: composer reformatted the prompt's whitespace but kept every "
                                "character — sending (%s)", name, _diagnose(state, text, name))
                if attempt:
                    log.info("%s: composer accepted the prompt on attempt %s", name, attempt + 1)
                return
            if time.monotonic() >= verify_deadline:
                break
            await asyncio.sleep(.1)

        if attempt + 1 < max(1, attempts) and time.monotonic() < deadline:
            log.warning("%s: composer did not hold the prompt on attempt %s, re-entering it (%s)",
                        name, attempt + 1, _diagnose(state, text, name))

    detail = _diagnose(state, text, name)
    log.error("%s: refusing to send — %s", name, detail)
    raise RuntimeError(f"{name}: full prompt was not preserved in the composer; not sending ({detail})")


async def submit_prompt(page, text, input_selectors, send_selectors, *, name="provider",
                        user_selectors=(), stop_selectors=(), timeout_ms=10000,
                        allow_enter=True, accepted_url_fragment=""):
    """Wait for an enabled Send control, click once, and require acknowledgment.

    Never retry a dispatched click: a delayed UI acknowledgment could otherwise
    create duplicate paid generations. A disabled button is waited on, never
    bypassed with Enter. A truly absent button can use one focused Enter.
    """
    args = {'inputs': _list(input_selectors), 'users': _list(user_selectors),
            'stops': _list(stop_selectors), 'text': text}
    before = await page.evaluate(_JS_STATE, args)
    expected = text.replace('\r\n', '\n').replace('\r', '\n')
    if not (before['draftMatches'] or before['draftSameContent']) or not expected.strip():
        detail = _diagnose(before, text, name)
        log.error("%s: refusing to send — %s", name, detail)
        raise RuntimeError(
            f"{name}: full prompt was not preserved in the composer; not sending ({detail})")
    if before['stopping']:
        raise RuntimeError(f"{name}: previous generation is still running; not sending")
    url_before = page.url
    deadline = time.monotonic() + timeout_ms / 1000
    found_button = False
    while True:
        button = await page.evaluate(_JS_BUTTON, _list(send_selectors))
        found_button = found_button or button['found']
        if 'x' in button:
            try:
                await page.mouse.click(button['x'], button['y'])
            except Exception as error:
                raise RuntimeError(f"{name}: submission outcome is uncertain; not retrying to avoid duplicates") from error
            break
        if time.monotonic() >= deadline:
            if found_button or not allow_enter:
                raise RuntimeError(f"{name}: Send control did not become ready; prompt was not sent")
            if not await page.evaluate(_JS_FOCUS, {'inputs': args['inputs'], 'select': False}):
                raise RuntimeError(f"{name}: composer lost before sending")
            try:
                await page.keyboard.press('Enter')
            except Exception as error:
                raise RuntimeError(f"{name}: submission outcome is uncertain; not retrying to avoid duplicates") from error
            break
        await asyncio.sleep(.1)
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            state = await page.evaluate(_JS_STATE, args)
        except Exception:
            # A send may navigate from the tool's home page to the result page.
            await asyncio.sleep(.1)
            continue
        if (state['draftEmpty'] or state['userMatches'] > before['userMatches'] or
                state['stopping'] or (page.url != url_before and
                (state['draft'] is None or (accepted_url_fragment and accepted_url_fragment in page.url)))):
            return
        await asyncio.sleep(.1)
    raise RuntimeError(f"{name}: submission was not acknowledged by the UI; not retrying to avoid duplicate generations")
