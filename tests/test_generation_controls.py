"""Offline regression tests; synthetic pages never contact provider accounts.

Run: python -m unittest tests.test_generation_controls -v
Set TEST_BROWSER_PATH to a Chrome/Chromium executable if Chrome is not installed.
"""

import asyncio
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from patchright.async_api import async_playwright

from src.api.worker_pool import Worker, _Acquired
from src.api.openai_schemas import ChatCompletionRequest
from src.api import openai_routes
from src.providers import dom_detector
from src.providers.base import chat_model_catalog, resolve_chat_model, model_id_to_provider
from src.providers.chatgpt import chat_models
from src.providers.model_picker import pick_from_dropdown
from src.providers.qwen.client import QwenClient
from src.providers.qwen import detector as qwen_detector
from src.providers import temporary_chat as privacy

log = logging.getLogger('tests')


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_resolves_every_chatgpt_choice(self):
        catalog = chat_model_catalog('chatgpt')
        self.assertEqual([m['id'] for m in catalog], list(chat_models.CHAT_MODEL_IDS))
        for entry in catalog:
            self.assertEqual(model_id_to_provider(entry['id']), 'chatgpt')
            if entry['id'] != 'chatgpt-browser':
                self.assertEqual(resolve_chat_model('chatgpt', entry['id']), entry['id'])
        self.assertEqual(resolve_chat_model('chatgpt', 'gpt-4o'), 'chatgpt-gpt-4o')
        self.assertTrue(chat_models.is_long('chatgpt-thinking'))
        self.assertEqual(resolve_chat_model('chatgpt', 'unknown'), '')

    async def test_failed_temporary_setup_releases_worker_without_sending(self):
        client = SimpleNamespace(new_chat=AsyncMock(side_effect=HTTPException(409)), send_message=AsyncMock())
        worker = SimpleNamespace(client=client)
        pool = SimpleNamespace(_acquire=AsyncMock(return_value=worker), _release=AsyncMock())
        token = privacy._required.set(True)
        try:
            with self.assertRaises(HTTPException):
                async with _Acquired(pool):
                    await client.send_message('private')
            client.send_message.assert_not_awaited()
            pool._release.assert_awaited_once_with(worker)
            self.assertTrue(worker.needs_fresh_chat)
        finally:
            privacy._required.reset(token)

    async def test_each_temporary_request_gets_a_new_chat_and_cleanup(self):
        client = SimpleNamespace(new_chat=AsyncMock())
        page = SimpleNamespace(goto=AsyncMock())
        worker = Worker(0, client, page, None, 'test', 'qwen')
        pool = SimpleNamespace(_acquire=AsyncMock(return_value=worker), _release=AsyncMock())
        token = privacy._required.set(True)
        try:
            for _ in range(2):
                async with _Acquired(pool):
                    worker.thread_message_count += 1
            self.assertEqual(client.new_chat.await_count, 2)
            self.assertEqual(page.goto.await_count, 2)
            self.assertEqual(worker.thread_message_count, 0)
            with self.assertRaises(HTTPException):
                await worker.navigate_to_thread('saved-thread')
        finally:
            privacy._required.reset(token)

    async def test_all_chat_policy_combinations_on_worker_checkout(self):
        for fresh, temporary in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(fresh=fresh, temporary=temporary), \
                 patch.object(privacy.Config, 'NEW_CHAT_EVERY_REQUEST', fresh), \
                 patch.object(privacy.Config, 'TEMPORARY_CHATS', temporary):
                client = SimpleNamespace(new_chat=AsyncMock())
                page = SimpleNamespace(goto=AsyncMock())
                worker = Worker(0, client, page, None, 'test', 'qwen')
                pool = SimpleNamespace(_acquire=AsyncMock(return_value=worker), _release=AsyncMock())
                worker.thread_message_count = 3

                async def app(scope, receive, send):
                    async with _Acquired(pool):
                        self.assertEqual(privacy.required(), temporary)
                        worker.increment_thread_count()

                await privacy.TemporaryChatMiddleware(app)(
                    {'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions', 'headers': []},
                    AsyncMock(), AsyncMock(),
                )
                self.assertEqual(client.new_chat.await_count, int(fresh or temporary))
                self.assertEqual(page.goto.await_count, int(temporary))
                self.assertEqual(worker.thread_message_count, 0 if temporary else 1 if fresh else 4)
                pool._release.assert_awaited_once_with(worker)

    async def test_request_flags_are_isolated_and_advertised(self):
        seen = {}
        fresh = {}
        async def app(scope, receive, send):
            await asyncio.sleep(0)
            seen[scope['test_id']] = privacy.required()
            fresh[scope['test_id']] = privacy.fresh_chat_required()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        middleware = privacy.TemporaryChatMiddleware(app)
        send = AsyncMock()
        with patch.object(privacy.Config, 'TEMPORARY_CHATS', False), \
             patch.object(privacy.Config, 'NEW_CHAT_EVERY_REQUEST', False):
            await asyncio.gather(*[
                middleware({'type': 'http', 'method': method, 'path': path,
                            'headers': [(b'x-temporary-chat', value)], 'test_id': key}, AsyncMock(), send)
                for key, method, path, value in [
                    ('chat', 'POST', '/v1/chat/completions', b'1'),
                    ('edit', 'POST', '/v1/qwen/images/edits', b'1'),
                    ('video', 'POST', '/v1/videos/generations', b'1'),
                    ('prefix', 'POST', '/temporary/v1/images/generations', b'0'),
                    ('off', 'POST', '/v1/chat/completions', b'0'),
                    ('read', 'GET', '/v1/models', b'1'),
                ]
            ])
        self.assertEqual(seen, {'chat': True, 'edit': True, 'video': True, 'prefix': True, 'off': False, 'read': False})
        self.assertEqual(fresh, {'chat': True, 'edit': True, 'video': True, 'prefix': True, 'off': False, 'read': False})
        self.assertFalse(privacy.required())
        self.assertFalse(privacy.fresh_chat_required())
        for call in send.await_args_list:
            self.assertIn((b'x-temporary-chats-supported', b'1'), call.args[0]['headers'])
        with patch.object(privacy.Config, 'TEMPORARY_CHATS', True):
            await middleware({'type': 'http', 'method': 'POST', 'path': '/chat',
                              'headers': [(b'x-temporary-chat', b'0')], 'test_id': 'global'}, AsyncMock(), send)
        self.assertTrue(seen['global'])
        self.assertTrue(fresh['global'])
        with patch.object(privacy.Config, 'TEMPORARY_CHATS', False), \
             patch.object(privacy.Config, 'NEW_CHAT_EVERY_REQUEST', True):
            await middleware({'type': 'http', 'method': 'POST', 'path': '/chat',
                              'headers': [], 'test_id': 'new-only'}, AsyncMock(), send)
        self.assertFalse(seen['new-only'])
        self.assertTrue(fresh['new-only'])

    async def test_qwen_returns_new_text_and_rejects_timeout_and_empty(self):
        page = SimpleNamespace(evaluate=AsyncMock(return_value=True), on=Mock(), remove_listener=Mock(),
                               locator=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=0))),
                               keyboard=SimpleNamespace(insert_text=AsyncMock(), press=AsyncMock()), url='https://chat.qwen.ai/c/test')
        client = QwenClient(page)
        client._enter_prompt = AsyncMock()
        client._submit_prompt = AsyncMock()
        client._select_chat_model = AsyncMock(return_value='qwen3-max')
        client._count_turns = AsyncMock(return_value=1)
        with patch('src.providers.text_client.random_delay', new=AsyncMock()), \
             patch.object(qwen_detector, 'wait_for_response_complete', new=AsyncMock(return_value=True)), \
             patch.object(qwen_detector, 'extract_latest_response', new=AsyncMock(return_value='Qwen text answer')) as extract:
            result = await client.send_message('Hello', chat_model='qwen3-max')
            self.assertEqual(result.message, 'Qwen text answer')
            self.assertFalse(result.has_images)
            worker = Worker(0, client, page, None, 'test', 'qwen')
            class Pool:
                def acquire(self, **kwargs):
                    assert kwargs['provider'] == 'qwen'
                    return _Acquired(self)
                async def _acquire(self, *args):
                    return worker
                async def _release(self, *args):
                    pass
            with patch.object(openai_routes, '_get_pool', return_value=Pool()), \
                 patch.object(openai_routes, '_usage', None):
                response = await openai_routes.create_chat_completion(ChatCompletionRequest(
                    model='qwen3-max', messages=[{'role': 'user', 'content': 'Hello'}]))
            self.assertEqual(response.choices[0].message.content, 'Qwen text answer')
            self.assertEqual(response.choices[0].finish_reason, 'stop')
            extract.return_value = ''
            with self.assertRaisesRegex(RuntimeError, 'empty'):
                await client.send_message('Hello')
        with patch('src.providers.text_client.random_delay', new=AsyncMock()), \
             patch.object(qwen_detector, 'wait_for_response_complete', new=AsyncMock(return_value=False)), \
             patch.object(qwen_detector, 'extract_latest_response', new=AsyncMock(return_value='STALE')) as extract:
            with self.assertRaisesRegex(RuntimeError, 'no complete new text'):
                await client.send_message('Hello')
            extract.assert_not_awaited()


class BrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv('TEST_BROWSER_PATH')
        self.browser = await self.pw.chromium.launch(headless=True, **({'executable_path': path} if path else {'channel': 'chrome'}))
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def _qwen_composer(self):
        await self.page.set_content('''
          <textarea id="chat-input"></textarea>
          <button id="send-message-button">Send</button><output id="sent">[]</output>
          <script>
          (() => {
            const input = document.querySelector('#chat-input');
            function send() {
              const output = document.querySelector('#sent');
              const messages = JSON.parse(output.textContent);
              messages.push(input.value);
              output.textContent = JSON.stringify(messages);
              input.value = '';
              const answer = document.createElement('div');
              answer.className = 'markdown-content-container';
              answer.textContent = 'Hello from Qwen';
              document.body.appendChild(answer);
            }
            input.addEventListener('keydown', event => {
              if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
            });
            document.querySelector('#send-message-button').onclick = send;
          })();
          </script>''')

    async def test_qwen_multiline_prompt_is_one_user_turn(self):
        # Real keyboard/input events, with Enter-to-send like Qwen. Mocking the
        # keyboard hid this bug: keyboard.type('\n') submits each partial line.
        await self._qwen_composer()
        client = QwenClient(self.page)
        client._select_chat_model = AsyncMock(return_value='qwen3-flash')
        prompt = '[SYSTEM]\nKeep context.\n\n[REQUEST]\nhi 👋'
        with patch('src.providers.text_client.random_delay', new=AsyncMock()):
            result = await client.send_message(prompt, chat_model='qwen3-flash')
        sent = await self.page.locator('#sent').text_content()
        self.assertEqual(json.loads(sent), [prompt])
        self.assertEqual(result.message, 'Hello from Qwen')

    async def test_long_prompt_replaces_stale_draft_in_both_editor_types(self):
        prompt = ('[STRICT ISOLATION]\nIgnore prior context.\n\n' * 40 +
                  '-----BEGIN REQUEST-----\nПривет 👋\n你好\n\nlast line\n')
        for composer in (
            '<textarea hidden id="hidden-draft">keep hidden</textarea><textarea id="chat-input">stale draft</textarea>',
            '<div id="chat-input" contenteditable="true">stale draft</div>',
        ):
            with self.subTest(composer=composer[:40]):
                await self.page.set_content(composer)
                client = QwenClient(self.page)
                # _enter_prompt performs its own full-draft equality check.
                await client._enter_prompt(prompt)
                actual = await self.page.locator('#chat-input').evaluate('''e => {
                    if (e instanceof HTMLTextAreaElement) return e.value;
                    const range = document.createRange();
                    range.selectNodeContents(e);
                    const selection = window.getSelection();
                    selection.removeAllRanges(); selection.addRange(range);
                    return selection.toString();
                }''')
                self.assertEqual(actual, prompt)

    async def test_qwen_does_not_submit_a_truncated_prompt(self):
        await self._qwen_composer()
        await self.page.locator('#chat-input').evaluate("e => e.addEventListener('input', () => { e.value = e.value.slice(0, 10); })")
        client = QwenClient(self.page)
        client._select_chat_model = AsyncMock(return_value='qwen3-flash')
        with self.assertRaisesRegex(RuntimeError, 'full prompt was not preserved'):
            await client.send_message('[System instruction]\n\nFull user request', chat_model='qwen3-flash')
        self.assertEqual(json.loads(await self.page.locator('#sent').text_content()), [])

    async def test_qwen_image_and_video_prompts_are_each_sent_once(self):
        prompt = '[ISOLATION]\nIgnore prior context.\n\nDraw a cat.\nНочной город 🐈'
        for kind in ('image', 'video'):
            with self.subTest(kind=kind):
                await self._qwen_composer()
                client = QwenClient(self.page)
                client.new_chat = AsyncMock()
                client._enter_media_mode = AsyncMock(return_value=(kind,))
                client._leave_media_mode = AsyncMock()
                with patch.object(qwen_detector, 'wait_for_new_media', new=AsyncMock(return_value=[])):
                    await client._run_media(kind=kind, mode_candidates=[(kind,)], prompt=prompt,
                                            image_paths=[], aspect_ratio='', timeout_ms=1000, settle_s=0, alt='test')
                self.assertEqual(json.loads(await self.page.locator('#sent').text_content()), [prompt])

    async def test_temporary_button_alone_is_not_proof(self):
        await self.page.set_content('<button aria-pressed="false" onclick="this.setAttribute(\'aria-pressed\', \'true\')">Temporary chat</button>')
        self.assertFalse(await self.page.evaluate(privacy._JS_TEMPORARY, 'check'))
        await self.page.evaluate(privacy._JS_TEMPORARY, 'enable')
        self.assertTrue(await self.page.evaluate(privacy._JS_TEMPORARY, 'check'))
        await self.page.set_content('<div data-message-author-role="assistant"><button aria-pressed="true">Temporary chat</button></div>')
        self.assertFalse(await self.page.evaluate(privacy._JS_TEMPORARY, 'check'))

    async def test_native_temporary_start_and_missing_mode(self):
        async def navigate(*args, **kwargs):
            await self.page.set_content('<button aria-pressed="false" onclick="this.setAttribute(\'aria-pressed\', \'true\')">Temporary chat</button>')
        token = privacy._required.set(True)
        try:
            with patch.object(self.page, 'goto', side_effect=navigate):
                await privacy.start_temporary_chat(self.page, 'qwen')
                await privacy.verify_temporary_chat(self.page)
            await self.page.set_content('<textarea id="chat-input"></textarea>')
            with patch.object(self.page, 'goto', new=AsyncMock()), \
                 patch.object(privacy.asyncio, 'sleep', new=AsyncMock()):
                with self.assertRaises(HTTPException) as raised:
                    await privacy.start_temporary_chat(self.page, 'qwen')
                self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException):
                await privacy.start_temporary_chat(self.page, 'seedream')
        finally:
            privacy._required.reset(token)

    async def test_picker_exact_title_and_legacy_submenu(self):
        await self.page.set_content('''<button id="picker" onclick="document.querySelector('#menu').hidden=false">ChatGPT</button>
          <div id="menu" hidden><button role="menuitem" onclick="document.querySelector('#legacy').hidden=false">Legacy models</button></div>
          <div id="legacy" hidden><button role="menuitem" onclick="document.body.dataset.picked='wrong'">GPT-4.1</button>
          <button role="menuitem" onclick="document.body.dataset.picked='right'">GPT-4o<br>Fast model</button></div>''')
        result = await pick_from_dropdown(self.page, open_selectors=['#picker'], item_selectors=['[role="menuitem"]'],
                                         labels=['GPT-4o'], submenu_labels=['Legacy models'], exact=True, settle_ms=1, log=log)
        self.assertTrue(result)
        self.assertEqual(await self.page.evaluate('document.body.dataset.picked'), 'right')
        await self.page.set_content('''<button id="picker">ChatGPT</button>
          <button role="menuitem" onclick="document.body.dataset.picked='wrong'">GPT-5.4</button>''')
        result = await pick_from_dropdown(self.page, open_selectors=['#picker'], item_selectors=['[role="menuitem"]'],
                                         labels=['GPT-5'], exact=True, settle_ms=1, log=log)
        self.assertEqual(result, '')
        self.assertIsNone(await self.page.evaluate('document.body.dataset.picked'))

    async def test_qwen_text_excludes_user_and_waits_while_stop_is_visible(self):
        await self.page.set_content('''<div class="user-message"><div class="markdown-content-container">USER</div></div>
          <div class="markdown-content-container">Older answer</div>
          <div class="markdown-content-container" id="answer">Thinking...</div>
          <button aria-label="Stop generating" id="stop">Stop</button>''')
        self.assertEqual(await qwen_detector.count_assistant_turns(self.page), 2)
        self.assertEqual(await qwen_detector.extract_latest_response(self.page), 'Thinking...')
        task = asyncio.create_task(qwen_detector.wait_for_response_complete(self.page, pre_count=1, timeout_ms=2000,
                                                                          poll_ms=20, stable_polls=2))
        await asyncio.sleep(0.15)
        self.assertFalse(task.done(), 'A paused reasoning answer must not be marked complete')
        await self.page.evaluate("document.querySelector('#answer').innerText='Final Qwen text'; document.querySelector('#stop').remove()")
        self.assertTrue(await task)
        self.assertEqual(await qwen_detector.extract_latest_response(self.page), 'Final Qwen text')
        self.assertFalse(await qwen_detector.wait_for_response_complete(self.page, pre_count=2, timeout_ms=80, poll_ms=10))


if __name__ == '__main__':
    unittest.main()
