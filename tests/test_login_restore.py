"""Restored sessions, late hydration and lifecycle races; no live accounts."""

import asyncio
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright
from src.accounts.manager import Account, AccountManager, AccountState
from src.accounts.monitor import AccountMonitor
from src.accounts.registry import AccountConfig
from src.accounts.usage import UsageStore
from src.api.worker_pool import MultiAccountPool
from src.core.browser.manager import BrowserManager
from src.core.browser.login import wait_for_login
from src.providers.base import get_provider, PROVIDERS


class LoginStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.usage = UsageStore(Path('unused-test-usage.json'))
        self.pool = MultiAccountPool()
        self.mgr = AccountManager(None, self.usage, self.pool)
        self.monitor = AccountMonitor(self.mgr, self.usage, self.pool)
        self.monitor._manage_overflow = AsyncMock()

    async def account(self, provider='qwen'):
        page = SimpleNamespace(goto=AsyncMock(), evaluate=AsyncMock(), bring_to_front=AsyncMock())
        bm = SimpleNamespace(is_logged_in=AsyncMock(return_value=True), page=page,
                             provider_url=lambda: get_provider(provider).url,
                             context=SimpleNamespace(clear_cookies=AsyncMock()), close=AsyncMock())
        acc = Account(AccountConfig(id=provider, provider=provider, tabs=1),
                      state=AccountState.LOGGED_OUT, manager=bm, started=True, login_pending=True)
        self.mgr._accounts[provider] = acc
        await self.pool.register_account(provider, 1, provider)
        return acc

    async def test_monitor_recovers_pending_logins_for_every_provider(self):
        accounts = [await self.account(p) for p in PROVIDERS]
        await self.monitor._tick(time.time())
        for acc in accounts:
            self.assertEqual(acc.state, AccountState.ACTIVE)
            self.assertTrue(self.pool._slots[acc.cfg.id].schedulable)

    async def test_startup_missed_session_recovers_on_a_later_tick(self):
        acc = await self.account()
        acc.started = False
        bm = acc.manager
        bm.start = AsyncMock(return_value=bm.page)
        bm.apply_stealth_patches = AsyncMock()
        bm.is_logged_in.side_effect = [False, False, True]
        self.mgr._build_spec = AsyncMock()
        self.mgr._navigate_with_retries = AsyncMock()
        with patch('src.accounts.manager.BrowserManager', return_value=bm), \
             patch('src.accounts.manager.asyncio.sleep', new=AsyncMock()):
            await self.mgr.start_account('qwen')
        self.assertEqual(acc.state, AccountState.LOGGED_OUT)
        self.assertFalse(self.pool._slots['qwen'].schedulable)
        await self.monitor._tick(time.time())
        self.assertEqual(acc.state, AccountState.LOGGED_OUT)
        await self.monitor._tick(time.time())
        self.assertEqual(acc.state, AccountState.ACTIVE)
        self.assertTrue(acc.workers[0].needs_fresh_chat)

    async def test_saved_cooldown_and_soft_cap_survive_login_confirmation(self):
        acc = await self.account()
        self.usage.note_limit('qwen', 3600)
        self.assertTrue(await self.mgr.recheck_login('qwen'))
        self.assertEqual(acc.state, AccountState.COOLDOWN)
        self.assertFalse(self.pool._slots['qwen'].schedulable)
        self.assertTrue(await self.mgr.confirm_login('qwen'))
        self.assertEqual(acc.state, AccountState.COOLDOWN)
        self.usage._get('qwen').cooldown_until = 0
        acc.cfg.soft_cap = 1
        self.usage.note_request('qwen')
        await self.mgr.confirm_login('qwen')
        self.assertEqual(acc.state, AccountState.COOLDOWN)

    async def test_stopped_disabled_failed_and_logged_out_accounts_are_not_promoted(self):
        acc = await self.account()
        for state in (AccountState.DISABLED, AccountState.FAILED, AccountState.COOLDOWN):
            acc.state = state
            self.assertFalse(await self.mgr.recheck_login('qwen'))
        acc.state = AccountState.LOGGED_OUT
        acc.cfg.enabled = False
        self.assertFalse(await self.mgr.recheck_login('qwen'))
        acc.cfg.enabled = True
        acc.started = False
        self.assertFalse(await self.mgr.recheck_login('qwen'))
        acc.manager.is_logged_in.assert_not_awaited()

    async def test_explicit_logout_stays_off_until_login_is_reopened(self):
        acc = await self.account()
        await self.mgr.logout('qwen')
        acc.manager.context.clear_cookies.assert_awaited_once()
        await self.monitor._tick(time.time())
        self.assertFalse(acc.login_pending)
        self.assertFalse(self.pool._slots['qwen'].schedulable)
        acc.manager.is_logged_in.assert_not_awaited()
        acc.manager.is_logged_in.return_value = False
        await self.mgr.open_login('qwen')
        self.assertTrue(acc.login_pending)
        acc.manager.is_logged_in.return_value = True
        await self.monitor._tick(time.time())
        self.assertEqual(acc.state, AccountState.ACTIVE)

    async def test_logout_and_stop_cannot_be_undone_by_an_inflight_probe(self):
        for operation in ('logout', 'stop_account'):
            acc = await self.account()
            entered, finish = asyncio.Event(), asyncio.Event()
            async def probe(**kwargs):
                entered.set()
                await finish.wait()
                return True
            acc.manager.is_logged_in.side_effect = probe
            check = asyncio.create_task(self.mgr.recheck_login('qwen'))
            await entered.wait()
            lifecycle = asyncio.create_task(getattr(self.mgr, operation)('qwen'))
            finish.set()
            await asyncio.gather(check, lifecycle)
            self.assertFalse(acc.login_pending)
            self.assertFalse(self.pool._slots['qwen'].schedulable)
            self.assertIn(acc.state, (AccountState.DISABLED, AccountState.LOGGED_OUT))

    async def test_cooldown_expiry_with_loading_page_is_rechecked(self):
        acc = await self.account()
        acc.state = AccountState.COOLDOWN
        acc.manager.is_logged_in.return_value = False
        await self.monitor._tick(time.time())
        self.assertEqual(acc.state, AccountState.LOGGED_OUT)
        acc.manager.is_logged_in.return_value = True
        await self.monitor._tick(time.time())
        self.assertEqual(acc.state, AccountState.ACTIVE)

    async def test_disabling_account_while_probe_runs_prevents_activation(self):
        acc = await self.account()
        await self.pool.set_schedulable('qwen', False)
        async def probe(**kwargs):
            acc.cfg.enabled = False
            return True
        acc.manager.is_logged_in.side_effect = probe
        self.assertFalse(await self.mgr.recheck_login('qwen'))
        self.assertEqual(acc.state, AccountState.LOGGED_OUT)
        self.assertFalse(self.pool._slots['qwen'].schedulable)


class LoginBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv('TEST_BROWSER_PATH')
        self.browser = await self.pw.chromium.launch(headless=True, **(
            {'executable_path': path} if path else {'channel': 'chrome'}))
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<body></body>'))
        self.page = await self.context.new_page()
        await self.page.goto('https://chat.qwen.ai/')
        self.bm = BrowserManager(SimpleNamespace(provider='qwen', account_id='qwen'))
        self.bm._context = self.context
        self.bm._page = self.page

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_restored_qwen_session_waits_for_anonymous_header_to_disappear(self):
        await self.page.set_content('''<button>Log in</button><textarea id="chat-input"></textarea>
            <script>setTimeout(()=>{document.querySelector('button').remove();
            const avatar=document.createElement('div');avatar.className='user-avatar';
            avatar.textContent='U';document.body.appendChild(avatar)},350)</script>''')
        self.assertTrue(await self.bm.is_logged_in(timeout_ms=1800))

    async def test_all_visible_login_buttons_are_checked_not_just_first_match(self):
        await self.page.set_content('''<button hidden>Log in</button><button>Sign in</button>
            <div class="user-avatar">U</div><textarea id="chat-input"></textarea>''')
        self.assertFalse(await self.bm.is_logged_in(timeout_ms=600))

    async def test_current_qwen_sidebar_menu_detects_login_without_old_avatar_markup(self):
        for menu in ('user-menu-btn', 'user-menu-btn-mobile'):
            await self.page.set_content(f'<div class="sidebar-user"><button class="{menu}"><img class="user-img" alt="User profile">User</button></div>')
            self.assertTrue(await self.bm.is_logged_in(timeout_ms=1200))

    async def test_anonymous_composer_is_not_a_qwen_or_gemini_login(self):
        for provider, html in [('qwen', '<textarea id="chat-input"></textarea>'),
                               ('gemini', '<div class="ql-editor" contenteditable="true">Ask</div>')]:
            spec = get_provider(provider)
            await self.page.goto(spec.url)
            await self.page.set_content(html)
            self.assertFalse(await wait_for_login(lambda: [self.page], spec, timeout_ms=600))

    async def test_another_account_tab_can_finish_login_even_if_primary_is_closed(self):
        second = await self.context.new_page()
        await second.goto('https://chat.qwen.ai/')
        await second.set_content('<div class="user-avatar">U</div>')
        await self.page.close()
        self.assertTrue(await self.bm.is_logged_in(timeout_ms=1000))

    async def test_foreign_origin_and_hidden_avatar_do_not_activate(self):
        await self.page.set_content('<div class="user-avatar" hidden>U</div>')
        second = await self.context.new_page()
        await second.goto('https://accounts.example.test/')
        await second.set_content('<div class="user-avatar">U</div>')
        self.assertFalse(await self.bm.is_logged_in(timeout_ms=600))

    async def test_transient_positive_followed_by_login_wall_is_not_accepted(self):
        await self.page.set_content('''<div class="user-avatar">U</div><script>
            setTimeout(()=>{const b=document.createElement('button');b.textContent='Log in';
            document.body.appendChild(b)},150)</script>''')
        self.assertFalse(await self.bm.is_logged_in(timeout_ms=700))
