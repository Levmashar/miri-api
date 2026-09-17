"""Native temporary chats. Never substitute a saved chat for temporary mode."""

import asyncio
from contextvars import ContextVar
from functools import wraps

from fastapi import HTTPException
from src.core.config import Config

_required = ContextVar("temporary_chat_required", default=False)
_fresh_chat_required = ContextVar("fresh_chat_required", default=False)


def required():
    return _required.get()


def fresh_chat_required():
    """Whether this request must begin in a new chat (saved or temporary)."""
    # ``required()`` keeps direct unit/integration callers that set the older
    # temporary context flag working: temporary mode always implies fresh.
    return _fresh_chat_required.get() or required()


def _error():
    return HTTPException(409, detail=(
        "Temporary chat mode is unavailable or could not be verified in this "
        "provider's web UI. No prompt was sent."
    ))


# Match controls by accessible names, never text inside a conversation. A plain
# 'Temporary' button is only an entry point, NOT proof that the mode is enabled.
_JS_TEMPORARY = r"""
(action) => {
    const visible = n => n && (n.offsetParent !== null || n.getClientRects().length > 0);
    const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const labels = ['temporary', 'temporary chat', 'incognito', 'incognito chat',
        'private chat', 'временный', 'временный чат', 'инкогнито', '临时聊天'];
    const controls = Array.from(document.querySelectorAll('button, [role="switch"], [role="menuitem"], [role="menuitemcheckbox"], a'))
        .filter(n => visible(n) && !n.disabled && !n.closest('[data-message-author-role], [data-testid^="conversation-turn"], [class*="response-message"], [class*="user-message"]'));
    const named = n => [n.innerText, n.getAttribute('aria-label'), n.getAttribute('title')]
        .some(s => labels.includes(norm(s)));
    const candidates = controls.filter(named);
    const on = n => ['aria-pressed', 'aria-checked'].some(a => n.getAttribute(a) === 'true') ||
        ['on', 'checked', 'active'].includes(n.getAttribute('data-state'));
    const exit = controls.some(n => /^(exit|turn off|leave) (temporary|incognito)( chat)?$/i.test(norm(n.getAttribute('aria-label') || n.innerText)));
    const badge = Array.from(document.querySelectorAll('[data-testid="temporary-chat-banner"], [data-testid="temporary-chat-badge"], [data-testid="incognito-banner"], main h1, main h2'))
        .some(n => visible(n) && !n.closest('[role="dialog"], [role="menu"], [data-message-author-role]') && labels.includes(norm(n.innerText)));
    if (candidates.some(on) || exit || badge) return true;
    if (action === 'enable') {
        const button = candidates.find(n => n.getAttribute('aria-disabled') !== 'true');
        if (button) button.click();
    }
    return false;
}
"""


async def verify_temporary_chat(page):
    if required() and not await page.evaluate(_JS_TEMPORARY, "check"):
        raise _error()


async def start_temporary_chat(page, provider):
    from src.providers.base import get_provider
    if provider == "seedream":
        raise _error()  # Dreamina is a project editor, not a temporary-chat UI.
    url = get_provider(provider).url
    if provider == "chatgpt":
        url = Config.CHATGPT_URL.rstrip('/') + '/?temporary-chat=true'
    elif provider == "claude":
        url = Config.CLAUDE_URL.rstrip('/') + '/new'
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(1)
    if await page.evaluate(_JS_TEMPORARY, "check"):
        return
    await page.evaluate(_JS_TEMPORARY, "enable")
    for _ in range(20):
        await asyncio.sleep(0.25)
        if await page.evaluate(_JS_TEMPORARY, "check"):
            return
    raise _error()


def temporary_new_chat(fn):
    """Also applies to new_chat calls made internally by image/video clients."""
    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        if required():
            provider = getattr(self, 'NAME', '') or self.__module__.split('.')[-2]
            await start_temporary_chat(self._page, provider.lower())
            if hasattr(self, '_modes_synced'):
                self._modes_synced = False
            return
        return await fn(self, *args, **kwargs)
    return wrapped


class TemporaryChatMiddleware:
    """Request-local chat-policy flags, including SSE and child tasks.

    Header support lets Miri enable the policy without changing other callers.
    The global env setting cannot be overridden by sending a false header.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        path = scope.get('path', '').rstrip('/')
        temporary_path = path.startswith('/temporary/')
        if temporary_path:
            path = path[len('/temporary'):]
            scope['path'] = path
            scope['raw_path'] = path.encode('utf-8')
        generation = scope.get('method') == 'POST' and (
            path.endswith(('/chat', '/thread/new', '/chat/completions', '/responses',
                           '/images/generations', '/images/edits', '/videos/generations'))
        )
        temporary = generation and (
            temporary_path or Config.TEMPORARY_CHATS or headers.get(b'x-temporary-chat') == b'1'
        )
        temporary_token = _required.set(temporary)
        fresh_token = _fresh_chat_required.set(
            generation and (temporary or Config.NEW_CHAT_EVERY_REQUEST)
        )

        async def send_with_capability(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []),
                    (b'x-temporary-chats-supported', b'1')]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_capability)
        finally:
            _fresh_chat_required.reset(fresh_token)
            _required.reset(temporary_token)
