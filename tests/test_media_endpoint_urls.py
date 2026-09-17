"""The media endpoints must hand back links the caller can actually fetch.

The regression: `/v1/videos/generations` returned the file's path on the
gateway's disk in `data[].url`. Clients follow that field, so a generation that
had rendered perfectly showed up as `GET /app/downloads/videos/….mp4 -> 404`.
"""

import unittest
import warnings
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

warnings.filterwarnings('ignore')

from fastapi.testclient import TestClient

from src.core.config import Config


class _Worker:
    account_id = 'acct-test'
    provider = 'qwen'

    def __init__(self, client):
        self.client = client

    def increment_thread_count(self):
        pass


class _Pool:
    def __init__(self, worker):
        self._worker = worker

    @asynccontextmanager
    async def acquire(self, *a, **kw):
        yield self._worker


class VideoUrlEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import src.api.server as server
        cls.routes = __import__('src.api.openai_routes', fromlist=['x'])
        cls.client = TestClient(server.app)
        cls.auth = {'Authorization': 'Bearer ' + (Config.API_TOKEN or 'x')}

    def setUp(self):
        Config.ensure_dirs()
        self.video = Config.VIDEOS_DIR / 'endpoint_clip_1234567890.mp4'
        self.video.write_bytes(b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 128)
        self.addCleanup(lambda: self.video.unlink(missing_ok=True))

    def _generate(self, **body):
        result = SimpleNamespace(
            videos=[SimpleNamespace(local_path=str(self.video), mime_type='video/mp4',
                                    duration_s=4.0, prompt_title='a cat')],
            message='', limit_hit=False)
        client = SimpleNamespace(generate_video=AsyncMock(return_value=result))
        pool = _Pool(_Worker(client))
        with patch.object(self.routes, '_get_pool', return_value=pool), \
             patch.object(self.routes, '_observe', new=AsyncMock()):
            return self.client.post('/v1/videos/generations',
                                    json={'model': 'qwen-video', 'prompt': 'a cat', **body},
                                    headers=self.auth)

    def test_returned_url_is_absolute_and_fetchable(self):
        r = self._generate()
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()['data'][0]

        self.assertTrue(item['url'].startswith('http'), item['url'])
        self.assertNotIn('/app/downloads/', item['url'])
        self.assertIn('/v1/files/videos/', item['url'])
        # The on-disk path is still reported, just not as something to fetch.
        self.assertEqual(item['local_path'], str(self.video))

        fetched = self.client.get('/v1/' + item['url'].split('/v1/', 1)[1])
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, self.video.read_bytes())
        self.assertEqual(fetched.headers['content-type'], 'video/mp4')

    def test_b64_form_is_unchanged(self):
        r = self._generate(response_format='b64_json')
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()['data'][0]
        self.assertIsNone(item['url'])
        self.assertTrue(item['b64_json'])


if __name__ == '__main__':
    unittest.main()
