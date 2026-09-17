"""
Shared UI model/mode picker — used by the chat providers that expose more than
one selectable model in their web UI (Gemini, Grok).

Why one module: the two UIs differ only in WHICH selectors open the dropdown and
which nodes are its entries. The hard part — matching a user-visible LABEL to a
menu entry and clicking it without tripping Playwright's actionability/scroll
hangs — is identical, and is exactly the approach already proven by the Gemini
"Redo with Nano Banana Pro" flow: match by TEXT (labels churn far less than
markup) and click through a direct DOM click inside page.evaluate().

Everything here is BEST-EFFORT by design. A model entry may be missing because
the account's plan does not offer it, or because the vendor renamed it since
this was written. In that case we log and leave the UI on whatever model it
already had, rather than failing a request that would otherwise be answered.
"""

from __future__ import annotations

import asyncio

# Matching is 3-pass, tried per candidate label in order: exact -> startswith ->
# contains. That ordering is what keeps "Grok 4" from selecting "Grok 4 Heavy"
# while still tolerating a UI that writes "Grok 4 (Auto)".
_JS_CLICK_BY_TEXT = """
(args) => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const visible = (n) => {
        if (!n) return false;
        if (n.disabled) return false;
        if (n.offsetParent !== null) return true;
        return !!(n.getClientRects && n.getClientRects().length);
    };
    // SELECTOR ORDER IS PRIORITY. Each selector is searched on its own, in the
    // order the provider listed them, so a precise selector ("the composer's
    // buttons") always wins over a loose fallback ("every button on the page").
    // Pooling them would let a sidebar "Search" button outrank the composer's
    // Search toggle purely because it appears earlier in the document.
    for (const sel of args.sels) {
        let nodes = [];
        try { nodes = Array.from(document.querySelectorAll(sel)); }
        catch (e) { continue; }
        const entries = nodes.filter(visible).map((n) => ({
            node: n,
            text: norm(n.innerText || n.textContent || n.getAttribute('aria-label')),
            title: norm((n.innerText || n.textContent || n.getAttribute('aria-label') || '').split('\\n')[0]),
        })).filter((e) => e.text);
        if (!entries.length) continue;

        for (const raw of args.labels) {
            const want = norm(raw);
            if (!want) continue;
            const tests = [
                (t) => t === want,
                (t) => t.startsWith(want),
                (t) => t.includes(want),
            ];
            for (const test of tests) {
                // Prefer the SHORTEST match: menu markup nests (an outer
                // container and the entry itself both match), and the shortest
                // text is the entry rather than the whole menu.
                const hits = entries.filter((e) => args.exact
                    ? e.text === want || e.title === want
                    : test(e.text));
                if (hits.length) {
                    hits.sort((a, b) => a.text.length - b.text.length);
                    hits[0].node.click();
                    return hits[0].text;
                }
            }
        }
    }
    return '';
}
"""

_JS_TRIGGER_TEXT = """
(sels) => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    for (const sel of sels) {
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { continue; }
        if (!el) continue;
        const t = norm(el.innerText || el.textContent || el.getAttribute('aria-label'));
        if (t) return t;
    }
    return '';
}
"""

_JS_CLICK_FIRST = """
(sels) => {
    for (const sel of sels) {
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { continue; }
        if (!el || el.disabled) continue;
        const shown = el.offsetParent !== null ||
                      !!(el.getClientRects && el.getClientRects().length);
        if (!shown) continue;
        el.click();
        return true;
    }
    return false;
}
"""


async def _click_by_text(page, selectors, labels, *, exact=False) -> str:
    try:
        return await page.evaluate(
            _JS_CLICK_BY_TEXT, {"sels": list(selectors), "labels": list(labels), "exact": exact}
        ) or ""
    except Exception:
        return ""


def _already_selected(trigger_text: str, labels) -> bool:
    """True when the picker's trigger already reads as the wanted model.

    EXACT match only, deliberately. A substring test would read a trigger of
    "Grok 4.1" as already being "Grok 4" and skip a switch the caller asked for.
    A missed shortcut only costs one extra menu open; a wrong skip silently
    answers on the wrong model.
    """
    t = " ".join((trigger_text or "").split()).lower()
    if not t:
        return False
    return any(t == " ".join(lab.split()).lower() for lab in labels if lab)


async def pick_from_dropdown(
    page,
    *,
    open_selectors,
    item_selectors,
    labels,
    log,
    what: str = "model",
    settle_ms: int = 600,
    submenu_labels=(),
    exact: bool = False,
) -> str:
    """Open a model dropdown and click the entry matching one of `labels`.

    Returns the label text actually clicked, "" if nothing matched (in which
    case the UI is left on its current model and the caller carries on).
    """
    if not labels:
        return ""

    # Cheap win: if the trigger already shows this model, don't touch the menu.
    try:
        current = await page.evaluate(_JS_TRIGGER_TEXT, list(open_selectors))
    except Exception:
        current = ""
    if _already_selected(current, labels):
        log.debug(f"{what}: already selected ({current!r})")
        return current

    try:
        opened = await page.evaluate(_JS_CLICK_FIRST, list(open_selectors))
    except Exception as e:
        log.debug(f"{what}: could not open the picker: {e}")
        return ""
    if not opened:
        log.info(f"{what}: no model picker on this page — keeping the current model")
        return ""

    await asyncio.sleep(settle_ms / 1000)
    clicked = await _click_by_text(page, item_selectors, labels, exact=exact)
    if not clicked and submenu_labels:
        opened_submenu = await _click_by_text(page, item_selectors, submenu_labels)
        if opened_submenu:
            await asyncio.sleep(settle_ms / 1000)
            clicked = await _click_by_text(page, item_selectors, labels, exact=exact)
    if clicked:
        log.info(f"{what}: selected {clicked!r}")
        await asyncio.sleep(settle_ms / 1000)
        return clicked

    log.warning(
        f"{what}: none of {list(labels)!r} is offered by this account — "
        "keeping the current model"
    )
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return ""


# Toggles ("DeepThink", "Search", "Think", "Deep Research") are STICKY: the UI
# remembers them for the tab, so a mode left on by one request would silently
# apply to the next. To switch one off again we have to know whether it is on,
# which these UIs express in several ways — aria-pressed/aria-checked, a
# data-state attribute, or an "active"/"selected" class. We read all of them,
# on the matched node and its nearest ancestors, and click only when the state
# actually differs from what was asked for.
_JS_SET_TOGGLE = """
(args) => {
    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const visible = (n) => {
        if (!n) return false;
        if (n.disabled) return false;
        if (n.offsetParent !== null) return true;
        return !!(n.getClientRects && n.getClientRects().length);
    };
    // Selector order is priority — see the note in _JS_CLICK_BY_TEXT. It matters
    // even more here: a mis-picked node gets CLICKED, which on these UIs could
    // mean opening a sidebar search instead of switching on a reasoning mode.
    let hit = null;
    for (const sel of args.sels) {
        let nodes = [];
        try { nodes = Array.from(document.querySelectorAll(sel)); }
        catch (e) { continue; }
        const entries = nodes.filter(visible).map((n) => ({
            node: n,
            text: norm(n.innerText || n.textContent || n.getAttribute('aria-label')),
        })).filter((e) => e.text);
        if (!entries.length) continue;

        for (const raw of args.labels) {
            const want = norm(raw);
            if (!want) continue;
            const tests = [
                (t) => t === want,
                (t) => t.startsWith(want),
                (t) => t.includes(want),
            ];
            for (const test of tests) {
                const hits = entries.filter((e) => test(e.text));
                if (hits.length) {
                    hits.sort((a, b) => a.text.length - b.text.length);
                    hit = hits[0];
                    break;
                }
            }
            if (hit) break;
        }
        if (hit) break;
    }
    if (!hit) return { found: false, text: '', state: null, clicked: false };

    // On-ness, searched on the node then up to 3 ancestors. null = unknowable.
    const readState = (el) => {
        for (let i = 0, n = el; i < 4 && n; i++, n = n.parentElement) {
            if (!n.getAttribute) continue;
            for (const attr of ['aria-pressed', 'aria-checked', 'aria-selected']) {
                const v = n.getAttribute(attr);
                if (v === 'true') return true;
                if (v === 'false') return false;
            }
            const ds = (n.getAttribute('data-state') || '').toLowerCase();
            if (['active', 'on', 'checked', 'selected'].includes(ds)) return true;
            if (['inactive', 'off', 'unchecked'].includes(ds)) return false;
            const raw = (n.className && n.className.baseVal !== undefined)
                ? n.className.baseVal : n.className;
            const cls = ' ' + ((typeof raw === 'string' ? raw : '') || '') + ' ';
            if (/(^|[\\s_-])(active|selected|enabled|checked)([\\s_-]|$)/i.test(cls)) return true;
        }
        return null;
    };

    const state = readState(hit.node);
    // Click when the state differs from what was asked for. When the state is
    // unknowable, click only to turn ON, or to turn OFF something WE turned on
    // (assumeOn) — never a blind click that could switch a mode on.
    let shouldClick = false;
    if (state === null) {
        shouldClick = args.desired ? true : !!args.assumeOn;
    } else {
        shouldClick = state !== args.desired;
    }
    if (shouldClick) hit.node.click();
    return { found: true, text: hit.text, state: state, clicked: shouldClick };
}
"""


async def _set_toggle(page, selectors, labels, desired: bool, assume_on: bool) -> dict:
    blank = {"found": False, "text": "", "state": None, "clicked": False}
    try:
        return await page.evaluate(
            _JS_SET_TOGGLE,
            {
                "sels": list(selectors),
                "labels": list(labels),
                "desired": bool(desired),
                "assumeOn": bool(assume_on),
            },
        ) or blank
    except Exception:
        return blank


async def set_mode(
    page,
    *,
    labels,
    button_selectors,
    desired: bool,
    menu_selectors=(),
    menu_item_selectors=(),
    assume_on: bool = False,
    log=None,
    what: str = "mode",
    settle_ms: int = 600,
) -> bool:
    """Put one composer MODE into `desired` state. True when it now holds.

    Modes sit next to the composer, sometimes behind a "Tools"/"+" menu: the
    visible buttons are tried first, then the menu is opened and tried again.
    Best-effort — a mode this account is not offered returns False and the
    request is sent without it.
    """
    if not labels:
        return False

    res = await _set_toggle(page, button_selectors, labels, desired, assume_on)
    if not res.get("found") and menu_selectors:
        try:
            opened = await page.evaluate(_JS_CLICK_FIRST, list(menu_selectors))
        except Exception:
            opened = False
        if opened:
            await asyncio.sleep(settle_ms / 1000)
            res = await _set_toggle(
                page, menu_item_selectors or button_selectors, labels, desired, assume_on
            )
            if not res.get("found"):
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass

    if not res.get("found"):
        if log and desired:
            log.warning(
                f"{what}: none of {list(labels)!r} is offered here — sending without it"
            )
        return False

    if res.get("clicked"):
        await asyncio.sleep(settle_ms / 1000)
        if log:
            log.info(f"{what}: {'enabled' if desired else 'disabled'} {res.get('text')!r}")
    elif log:
        log.debug(f"{what}: {res.get('text')!r} already {'on' if desired else 'off'}")
    return True


async def apply_modes(
    page,
    *,
    wanted_groups,
    active_groups,
    button_selectors,
    menu_selectors=(),
    menu_item_selectors=(),
    assume_on: bool = True,
    log=None,
    what: str = "mode",
) -> "tuple[bool, list]":
    """Enable the modes this request wants and switch off the ones it doesn't.

    `active_groups` is what THIS tab last had switched on (the caller keeps it
    across requests, because the UI keeps the toggles across requests too).
    `assume_on` says whether those are known-on: True for modes we ourselves
    switched on, False when they are merely SUSPECTED — the first request on a
    tab, where a persistent profile may have carried a mode over from an earlier
    session. With assume_on=False a switch whose state cannot be read is left
    alone rather than blind-clicked (which would turn it ON).
    Returns (any_enabled, new_active_groups).
    """
    wanted = [tuple(g) for g in (wanted_groups or [])]
    active = [tuple(g) for g in (active_groups or [])]

    # Off first, so a tab that had DeepThink on doesn't answer a plain request
    # in reasoning mode. assume_on: we turned these on, so an unreadable state
    # still means "click to clear".
    for group in active:
        if group in wanted:
            continue
        # Only dig through the tools menu for a mode we KNOW we switched on
        # (assume_on). On the speculative first-sync pass, a mode that isn't
        # even visible can't be on in a way that matters, and opening every
        # menu to check would cost a round of clicks on every tab's first
        # request.
        await set_mode(
            page, labels=group, button_selectors=button_selectors,
            menu_selectors=menu_selectors if assume_on else (),
            menu_item_selectors=menu_item_selectors,
            desired=False, assume_on=assume_on, log=log, what=what,
        )

    now_on = []
    for group in wanted:
        if await set_mode(
            page, labels=group, button_selectors=button_selectors,
            menu_selectors=menu_selectors, menu_item_selectors=menu_item_selectors,
            desired=True, log=log, what=what,
        ):
            now_on.append(group)
    return bool(now_on), now_on





async def click_confirm(page, *, labels, selectors, log=None, timeout_ms: int = 25000,
                        poll_ms: int = 700) -> str:
    """Poll for a confirmation button (e.g. Gemini's "Start research") and click it.

    Some modes answer with a PLAN first and only start the real work once the
    user confirms. Returns the clicked label, "" on timeout.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        clicked = await _click_by_text(page, selectors, labels)
        if clicked:
            if log:
                log.info(f"confirmed: {clicked!r}")
            return clicked
        await asyncio.sleep(poll_ms / 1000)
    if log:
        log.info(f"no confirmation button {list(labels)!r} appeared — continuing")
    return ""
