import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.api.request_log import RequestLogMiddleware, RequestLogStore


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    async def add(self, store, **overrides):
        values = dict(method='POST', path='/v1/videos/generations', status=200, duration_ms=20,
            req_headers=[(b'content-type', b'application/json'), (b'authorization', b'Bearer secret')],
            req_body=b'{"model":"qwen-video"}', req_size=22, req_captured=True,
            resp_headers=[(b'content-type', b'application/json')], resp_body=b'{}', resp_size=2,
            client='127.0.0.1', streaming=False)
        values.update(overrides)
        return await store.add(**values)

    async def test_restart_preserves_redacted_records_and_marks_unfinished(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.sqlite3'
            store = RequestLogStore(db_path=path)
            media = json.dumps({'image': 'data:image/png;base64,'+'A'*400}).encode()
            eid = await self.add(store, req_body=media, req_size=len(media), state='in_progress')
            store.close()
            store = RequestLogStore(db_path=path)
            entry = store.get(eid)
            self.assertEqual(entry['state'], 'interrupted')
            self.assertEqual(entry['status'], 503)
            self.assertNotIn('A'*400, entry['req_body'])
            self.assertEqual(entry['req_headers']['authorization'], '***redacted***')
            self.assertGreater(await self.add(store), eid)
            await store.clear()
            store.close()
            store = RequestLogStore(db_path=path)
            self.assertEqual(store.count(), 0)
            store.close()

    async def test_pagination_and_filter_cover_more_than_500_requests(self):
        store = RequestLogStore()
        for i in range(510):
            await self.add(store, path='/v1/older-match' if i == 0 else '/v1/videos/generations')
        first = store.page(500)
        self.assertEqual(len(first['entries']), 500)
        await self.add(store)
        second = store.page(500, before=first['next_before'])
        ids = [e['id'] for e in first['entries'] + second['entries']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 510)
        self.assertEqual(store.page(500, query='older-match')['matched'], 1)
        self.assertEqual(store.page(500, query='older-match')['entries'][0]['id'], 1)

    async def test_disk_count_bound_and_unavailable_database_are_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.sqlite3'
            store = RequestLogStore(db_path=path, max_entries=3)
            for _ in range(6):
                await self.add(store)
            store.close()
            store = RequestLogStore(db_path=path, max_entries=3)
            self.assertEqual([row['id'] for row in store.summaries()], [6, 5, 4])
            store.close()
            path.write_bytes(b'not a database')
            store = RequestLogStore(db_path=path)
            await self.add(store)
            self.assertEqual(store.count(), 1)
            self.assertFalse(store.page()['persistent'])
            self.assertIn('only in memory', store.page()['warning'])

    async def test_pending_is_visible_then_updated_once_without_buffering_request(self):
        store = RequestLogStore()
        ready, finish = asyncio.Event(), asyncio.Event()
        body = b'{"model":"qwen-video","prompt":"cat"}'
        async def app(scope, receive, send):
            self.assertEqual(store.summaries()[0]['state'], 'in_progress')
            self.assertEqual((await receive())['body'], body)
            ready.set()
            await finish.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{"ok":true}'})
        scope = dict(type='http', method='POST', path='/v1/videos/generations', headers=[(b'content-type', b'application/json')])
        task = asyncio.create_task(RequestLogMiddleware(app, store)(scope,
            AsyncMock(return_value={'type': 'http.request', 'body': body}), AsyncMock()))
        await ready.wait()
        row = store.summaries()[0]
        self.assertEqual(row['model'], 'qwen-video')
        self.assertEqual(row['state'], 'in_progress')
        finish.set()
        await task
        self.assertEqual(store.count(), 1)
        self.assertEqual(store.get(row['id'])['state'], 'completed')
        self.assertEqual(store.get(row['id'])['resp_body'], '{"ok":true}')

    async def test_disconnect_upload_and_uncaught_exception_are_logged(self):
        for failure in ('disconnect', 'exception'):
            store = RequestLogStore()
            async def app(scope, receive, send):
                await receive()
                if failure == 'exception':
                    raise RuntimeError('private error')
            scope = dict(type='http', method='POST', path='/v1/qwen/images/edits',
                         headers=[(b'content-type', b'multipart/form-data')])
            middleware = RequestLogMiddleware(app, store)
            receive = AsyncMock(return_value={'type': 'http.disconnect'} if failure == 'disconnect' else
                                {'type': 'http.request', 'body': b'upload bytes'})
            if failure == 'exception':
                with self.assertRaises(RuntimeError):
                    await middleware(scope, receive, AsyncMock())
            else:
                await middleware(scope, receive, AsyncMock())
            entry = store.get(1)
            self.assertEqual(entry['status'], 499 if failure == 'disconnect' else 500)
            self.assertNotIn('private error', entry['error'])
            self.assertNotIn('upload bytes', entry['req_body'])

    async def test_streaming_error_preserves_partial_response_and_invalid_model_is_logged(self):
        store = RequestLogStore()
        async def app(scope, receive, send):
            await receive()
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-type', b'text/event-stream')]})
            await send({'type': 'http.response.body', 'body': b'data: partial\n\n', 'more_body': True})
            raise ValueError('stream broke')
        scope = dict(type='http', method='POST', path='/v1/chat/completions', headers=[(b'content-type', b'application/json')])
        with self.assertRaises(ValueError):
            await RequestLogMiddleware(app, store)(scope,
                AsyncMock(return_value={'type': 'http.request', 'body': b'{"model": []}'}), AsyncMock())
        entry = store.get(1)
        self.assertEqual(entry['state'], 'failed')
        self.assertTrue(entry['streaming'])
        self.assertIn('partial', entry['resp_body'])

    async def test_export_carries_full_bodies_and_survives_a_foreign_thread(self):
        """The download must hold whole records, and the database must accept
        writes from the serving thread even when another one opened it."""
        import threading
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'requests.sqlite3'
            store = None

            def build():
                nonlocal store
                store = RequestLogStore(db_path=path)

            opener = threading.Thread(target=build)   # NOT the thread that writes
            opener.start()
            opener.join()

            for i in range(4):
                await self.add(store, resp_body=json.dumps({'detail': f'boom {i}'}).encode(),
                               resp_size=20, status=500)
            self.assertEqual(store.persistence_error, '')

            exported = store.export(2)
            self.assertEqual(len(exported), 2)
            self.assertIn('boom 3', exported[0]['resp_body'])        # newest first
            self.assertIn('req_headers', exported[0])
            self.assertNotIn('secret', json.dumps(exported))         # still redacted
            self.assertEqual(len(store.export(50)), 4)               # more asked than stored
            self.assertEqual(store.export(50, query='videos')[0]['path'], '/v1/videos/generations')
            self.assertEqual(store.export(50, query='/v1/chat'), [])

            # Records really reached disk, not just memory.
            store.close()
            reopened = RequestLogStore(db_path=path)
            self.assertEqual(reopened.count(), 4)
            reopened.close()
