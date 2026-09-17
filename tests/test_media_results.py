"""Completed media, download retries, and current Qwen DOM regression cases."""
import asyncio
import base64
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from patchright.async_api import async_playwright
from src.providers import media_fetch
from src.providers.qwen.media_capture import QwenMediaCapture
from src.providers.qwen import detector


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_download_tries_alternate_source_without_resubmitting(self):
        from src.providers.qwen.client import QwenClient
        client = QwenClient(Mock())
        client.new_chat = AsyncMock()
        client._enter_media_mode = AsyncMock(return_value=('video',))
        client._leave_media_mode = AsyncMock()
        client._enter_prompt = AsyncMock()
        client._submit_prompt = AsyncMock()
        with patch.object(detector, 'capture_media_srcs', new=AsyncMock(return_value=set())), \
             patch.object(detector, 'wait_for_new_media', new=AsyncMock(return_value=['https://cdn.test/expired', 'blob:valid'])), \
             patch('src.providers.qwen.client.download_media', new=AsyncMock(side_effect=[('', ''), ('saved.mp4', 'video/mp4')])) as download:
            result = await client._run_media(kind='video', mode_candidates=[('video',)], prompt='animate',
                image_paths=[], aspect_ratio='', timeout_ms=100, settle_s=0, alt='test', max_items=1)
        self.assertEqual(result[0], [('blob:valid', 'saved.mp4', 'video/mp4')])
        self.assertEqual(download.await_count, 2)
        client._submit_prompt.assert_awaited_once_with('animate')

    async def test_only_matching_prompt_task_can_produce_media_even_with_raced_status(self):
        page = SimpleNamespace(on=Mock(), remove_listener=Mock())
        capture = QwenMediaCapture(page, 'animate this\ncat', 'video')
        wrong = SimpleNamespace(method='POST', url='https://chat.qwen.ai/api/v2/chat/completions',
                                post_data_json={'messages': [{'role': 'user', 'content': 'other request'}]})
        capture._request(wrong)
        self.assertFalse(capture.requests)
        request = Mock(method='POST', url=wrong.url,
                       post_data_json={'messages': [{'role': 'user', 'content': 'animate this\ncat'}]})
        capture._request(request)
        self.assertIn(request, capture.requests)
        capture._status('foreign-task', {'task_status': 'success', 'content': 'https://cdn.test/other.mp4'})
        capture._status('mine', {'task_status': 'success', 'content': 'https://cdn.test/result.mp4'})
        self.assertFalse(capture.urls)
        capture._completion({'data': {'messages': [{'role': 'assistant', 'extra': {'wanx': {'task_id': 'mine'}}}]}})
        self.assertEqual(capture.urls, ['https://cdn.test/result.mp4'])
        capture._status('foreign-task', {'task_status': 'failed'})
        self.assertFalse(capture.error)

    async def test_image_requires_finished_phase_and_task_failure_is_explicit(self):
        capture = QwenMediaCapture(None, 'draw', 'image')
        event = {'choices': [{'delta': {'phase': 'image_gen', 'content': 'https://cdn.test/image.png'}}]}
        capture._completion(event)
        self.assertFalse(capture.urls)
        event['choices'][0]['delta']['status'] = 'finished'
        capture._completion(event)
        self.assertEqual(capture.urls, ['https://cdn.test/image.png'])
        capture.task_ids.add('task')
        capture._status('task', {'task_status': 'failed'})
        self.assertIn('failed', capture.error)

    async def test_browser_response_listeners_read_sse_and_are_removed(self):
        page = SimpleNamespace(on=Mock(), remove_listener=Mock())
        async with QwenMediaCapture(page, 'draw', 'image') as capture:
            request = Mock(method='POST', url='https://chat.qwen.ai/api/v2/chat/completions',
                           post_data_json={'messages': [{'role': 'user', 'content': 'draw'}]})
            capture._request(request)
            event = {'choices': [{'delta': {'phase': 'image_gen', 'status': 'finished',
                                           'content': 'https://cdn.test/done.png'}}]}
            response = Mock(request=request, url=request.url, status=200,
                            body=AsyncMock(return_value=('data: '+json.dumps(event)+'\n\n').encode()))
            capture._response(response)
            await asyncio.gather(*capture.tasks)
            self.assertEqual(capture.urls, ['https://cdn.test/done.png'])
        self.assertEqual(page.remove_listener.call_count, 2)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_exception_still_uses_page_fallback(self):
        data = b'\x89PNG\r\n\x1a\n' + b'fixture'
        page = SimpleNamespace(url='https://chat.qwen.ai/c/test',
            context=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError))),
            evaluate=AsyncMock(return_value={'b64': base64.b64encode(data).decode(), 'type': 'image/png'}))
        self.assertEqual(await media_fetch.fetch_bytes(page, 'https://cdn.test/image'), (data, 'image/png'))
        page.evaluate.assert_awaited_once()

    async def test_http_200_error_body_is_not_saved_and_retry_succeeds(self):
        data = b'\x00\x00\x00\x18ftypmp42' + b'fixture'
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(media_fetch.Config, 'VIDEOS_DIR', Path(folder)), \
             patch.object(media_fetch.Config, 'ensure_dirs'), \
             patch.object(media_fetch, 'fetch_bytes', new=AsyncMock(side_effect=[(b'<html>not ready</html>', 'text/html'), (data, 'application/octet-stream')])), \
             patch.object(media_fetch.asyncio, 'sleep', new=AsyncMock()):
            path, mime = await media_fetch.download_media(None, 'https://cdn.test/clip', kind='video')
            self.assertEqual(Path(path).read_bytes(), data)
            self.assertEqual(mime, 'video/mp4')

    async def test_context_response_disposed_and_html_falls_back(self):
        response = SimpleNamespace(ok=True, status=200, headers={'content-type': 'text/html'},
                                   body=AsyncMock(return_value=b'<html>error</html>'), dispose=AsyncMock())
        page = SimpleNamespace(url='https://chat.qwen.ai/', context=SimpleNamespace(
            request=SimpleNamespace(get=AsyncMock(return_value=response))))
        with patch.object(media_fetch, '_in_page', new=AsyncMock(return_value=(b'bytes', 'video/mp4'))) as fallback:
            self.assertEqual(await media_fetch.fetch_bytes(page, 'https://cdn.test/clip'), (b'bytes', 'video/mp4'))
            fallback.assert_awaited_once()
        response.dispose.assert_awaited_once()

    async def test_invalid_media_exhausts_retries_without_writing(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(media_fetch.Config, 'IMAGES_DIR', Path(folder)), \
             patch.object(media_fetch, 'fetch_bytes', new=AsyncMock(return_value=(b'{"error":"expired"}', 'application/json'))) as fetch, \
             patch.object(media_fetch.asyncio, 'sleep', new=AsyncMock()):
            self.assertEqual(await media_fetch.download_media(None, 'https://cdn.test/x', kind='image'), ('', ''))
            self.assertEqual(fetch.await_count, 3)
            self.assertEqual(list(Path(folder).iterdir()), [])


class MediaDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv('TEST_BROWSER_PATH')
        self.browser = await self.pw.chromium.launch(headless=True, **(
            {'executable_path': path} if path else {'channel': 'chrome'}))
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_video_source_child_and_generation_overlay(self):
        await self.page.set_content('<div class="response-message"><div class="qwen-video"><video><source src="https://cdn.test/new.mp4"></video><div class="qwen-video-generating"></div></div></div>')
        await self.page.evaluate("Object.defineProperty(document.querySelector('video'), 'currentSrc', {value:'https://cdn.test/stale.mp4'})")
        self.assertEqual(await detector.wait_for_new_media(self.page, kind='video', baseline=set(),
            timeout_ms=100, settle_s=0, poll_ms=20), [])
        await self.page.locator('.qwen-video-generating').evaluate('(el)=>el.remove()')
        self.assertEqual(await detector.wait_for_new_media(self.page, kind='video', baseline=set(),
            timeout_ms=500, settle_s=0, poll_ms=20), ['https://cdn.test/new.mp4'])

    async def test_small_native_image_waits_for_overlay_and_ignores_video_cover(self):
        await self.page.set_content('<div class="response-message"><div class="qwen-image"><img src="https://cdn.test/image.png"><div class="qwen-image-generating"></div></div><div class="qwen-video"><img class="video-cover" src="https://cdn.test/poster.png"></div></div>')
        await self.page.evaluate('''() => { for (const img of document.images) {
            Object.defineProperties(img, {complete:{value:true}, naturalWidth:{value:200}, naturalHeight:{value:400}});
        }}''')
        self.assertEqual(await detector.wait_for_new_media(self.page, kind='image', baseline=set(),
            timeout_ms=100, settle_s=0, poll_ms=20), [])
        await self.page.locator('.qwen-image-generating').evaluate('(el)=>el.remove()')
        self.assertEqual(await detector.wait_for_new_media(self.page, kind='image', baseline=set(),
            timeout_ms=500, settle_s=0, poll_ms=20), ['https://cdn.test/image.png'])

    async def test_task_result_succeeds_without_mounted_player(self):
        await self.page.set_content('<div class="response-message">Generating video…</div>')
        capture = QwenMediaCapture(None, 'animate', 'video')
        async def finish():
            await asyncio.sleep(.05)
            capture.urls = ['https://cdn.test/task.mp4']
        task = asyncio.create_task(finish())
        result = await detector.wait_for_new_media(self.page, kind='video', baseline=set(),
            timeout_ms=500, settle_s=0, poll_ms=20, capture=capture)
        await task
        self.assertEqual(result, ['https://cdn.test/task.mp4'])

    async def test_actual_logs_ui_loads_older_and_searches_all_entries(self):
        from src.api.admin_html import ADMIN_HTML
        from src.api.request_log import RequestLogStore
        store = RequestLogStore()
        for i in range(505):
            await store.add(method='POST', path='/v1/qwen/videos/generations' if i == 0 else '/v1/chat/completions',
                status=200, duration_ms=10, req_headers=[], req_body=b'', req_size=0, req_captured=True,
                resp_headers=[], resp_body=b'', resp_size=0, client='', streaming=False)
        async def route(handler):
            url = urlparse(handler.request.url)
            if url.path == '/admin/api/logs':
                args = parse_qs(url.query)
                data = store.page(int(args.get('limit', ['200'])[0]),
                                  before=int(args['before'][0]) if 'before' in args else None,
                                  query=args.get('q', [''])[0])
                await handler.fulfill(json=data)
            else:
                await handler.fulfill(body='<html></html>', content_type='text/html')
        await self.page.route('**/*', route)
        await self.page.goto('https://fixture.test/admin')
        html = re.sub(r'<script>.*?</script>', '', ADMIN_HTML, flags=re.S)
        script = ADMIN_HTML.split('// ── Logs viewer ──')[1].split('if(!token())')[0]
        await self.page.set_content(html + '<script>' + '''
            const $=s=>document.querySelector(s);
            const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
            const api=async p=>(await fetch(p)).json();
        ''' + script + '\nopenLogs();</script>')
        await self.page.wait_for_function("document.querySelector('#log-count').textContent.startsWith('500 shown')")
        self.assertEqual(await self.page.locator('#log-rows tr').count(), 500)
        await self.page.locator('#log-more').click()
        await self.page.wait_for_function("document.querySelector('#log-count').textContent.startsWith('505 shown')")
        self.assertEqual(await self.page.locator('#log-rows tr').count(), 505)
        await self.page.locator('#log-filter').fill('qwen')
        await self.page.wait_for_function("document.querySelector('#log-count').textContent.startsWith('1 shown')")
        self.assertIn('qwen', await self.page.locator('#log-rows').inner_text())
        self.assertEqual(await self.page.locator('#log-rows tr').count(), 1)
        await self.page.locator('#logsModal').get_by_role('button', name='Close', exact=True).click()
