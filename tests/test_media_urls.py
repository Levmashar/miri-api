"""Generated media must come back as a URL that actually resolves.

The regression these cover: `data[].url` used to be the file's path on the
gateway's disk, so a client following it asked its own gateway for
`/app/downloads/videos/clip.mp4` and got a 404 with the video sitting right
there on disk.
"""

import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from starlette.datastructures import Headers

from src.api import media_files
from src.core.config import Config


class _Req:
    """Just enough of a Starlette request for public_base()."""

    def __init__(self, headers=None, scheme='http'):
        self.headers = Headers(headers or {'host': 'gw.example:8000'})
        self.url = type('U', (), {'scheme': scheme})()
        self.base_url = f'{scheme}://gw.example:8000/'


class MediaUrlTests(unittest.TestCase):
    def setUp(self):
        Config.ensure_dirs()
        self.video = Config.VIDEOS_DIR / 'unit_clip_0123456789.mp4'
        self.video.write_bytes(b'\x00\x00\x00\x18ftypmp42')
        self.addCleanup(lambda: self.video.unlink(missing_ok=True))

    def test_url_is_absolute_signed_and_honours_forwarded_headers(self):
        url = media_files.media_url(str(self.video), kind='videos', request=_Req())
        parsed = urlparse(url)
        self.assertEqual((parsed.scheme, parsed.netloc), ('http', 'gw.example:8000'))
        self.assertEqual(parsed.path, f'/v1/files/videos/{self.video.name}')
        self.assertIn(media_files.sign('videos', self.video.name), parsed.query)

        forwarded = media_files.media_url(str(self.video), kind='videos', request=_Req(
            {'host': 'internal:8000', 'x-forwarded-proto': 'https',
             'x-forwarded-host': 'api.example.com'}))
        self.assertTrue(forwarded.startswith('https://api.example.com/v1/files/videos/'))

        with patch.object(Config, 'PUBLIC_BASE_URL', 'https://pinned.example/'):
            pinned = media_files.media_url(str(self.video), kind='videos', request=_Req())
        self.assertTrue(pinned.startswith('https://pinned.example/v1/files/videos/'))

    def test_a_file_outside_the_media_directories_is_never_dressed_up_as_a_url(self):
        outside = Config.PROJECT_ROOT / '.env'
        self.assertEqual(media_files.media_url(str(outside), kind='videos', request=_Req()), '')
        self.assertEqual(media_files.media_url('', kind='videos', request=_Req()), '')
        self.assertEqual(media_files.media_url(str(self.video), kind='secrets', request=_Req()), '')
        self.assertEqual(
            media_files.media_url(str(Config.VIDEOS_DIR / 'sp ace/../../x.mp4'),
                                  kind='videos', request=_Req()), '')

    def test_signature_is_per_file_and_keyed_on_the_token(self):
        self.assertNotEqual(media_files.sign('videos', 'a.mp4'), media_files.sign('videos', 'b.mp4'))
        self.assertNotEqual(media_files.sign('videos', 'a.mp4'), media_files.sign('images', 'a.mp4'))
        with patch.object(Config, 'API_TOKEN', 'another-token'):
            rotated = media_files.sign('videos', 'a.mp4')
        self.assertNotEqual(media_files.sign('videos', 'a.mp4'), rotated)


class MediaRouteTests(unittest.TestCase):
    """End-to-end through the real app, including the auth middleware."""

    @classmethod
    def setUpClass(cls):
        import warnings
        warnings.filterwarnings('ignore')
        from fastapi.testclient import TestClient
        import src.api.server as server
        cls.client = TestClient(server.app)

    def setUp(self):
        Config.ensure_dirs()
        self.image = Config.IMAGES_DIR / 'unit_shot_9876543210.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32)
        self.addCleanup(lambda: self.image.unlink(missing_ok=True))
        self.sig = media_files.sign('images', self.image.name)

    def url(self, name, sig):
        return f'/v1/files/images/{name}?sig={sig}'

    def test_signed_link_is_served_without_an_api_key(self):
        # No Authorization header: a browser <img src> cannot send one.
        r = self.client.get(self.url(self.image.name, self.sig))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['content-type'], 'image/png')
        self.assertEqual(r.content, self.image.read_bytes())
        self.assertTrue(r.headers.get('content-disposition', '').startswith('inline'))

    def test_link_also_works_under_the_api_key_path_prefix(self):
        if not Config.API_TOKEN:
            self.skipTest('no API token configured')
        r = self.client.get(f'/{Config.API_TOKEN}' + self.url(self.image.name, self.sig))
        self.assertEqual(r.status_code, 200)

    def test_wrong_signature_missing_file_and_traversal_all_404(self):
        for path in (self.url(self.image.name, 'deadbeef'),
                     self.url(self.image.name, ''),
                     self.url('gone_0000000000.png', media_files.sign('images', 'gone_0000000000.png')),
                     '/v1/files/images/..%2F..%2F.env?sig=' + media_files.sign('images', '../../.env'),
                     '/v1/files/secrets/x.png?sig=' + media_files.sign('secrets', 'x.png')):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_404_body_explains_expiry_rather_than_leaking_paths(self):
        r = self.client.get(self.url('gone_1111111111.png', media_files.sign('images', 'gone_1111111111.png')))
        detail = r.json()['detail']
        self.assertIn('b64_json', detail)
        self.assertNotIn(str(Config.IMAGES_DIR), detail)


if __name__ == '__main__':
    unittest.main()
