"""Real-browser fixtures for Gemini/Grok completion; no provider accounts used."""

import os
import unittest

from patchright.async_api import async_playwright

from src.providers.gemini import detector as gemini
from src.providers.grok import detector as grok


class ProviderDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv("TEST_BROWSER_PATH")
        self.browser = await self.pw.chromium.launch(
            headless=True, **({"executable_path": path} if path else {"channel": "chrome"})
        )
        self.page = await self.browser.new_page()
        await self.page.route("https://fixture.invalid/**", lambda route: route.abort())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def _reply(self, provider, text="", *, stop=""):
        turn = (f'<model-response id="reply"><message-content>{text}</message-content></model-response>'
                if provider is gemini else f'<div id="reply" class="message-bubble">{text}</div>')
        await self.page.set_content(
            '<style>model-response,message-content{display:block}</style>' + turn + stop
        )

    async def _image(self):
        # A decoded, real image with a blob URL, matching both extractors' input.
        await self.page.evaluate("""async () => {
            const image = new Image();
            const ready = new Promise((resolve, reject) => {
                image.onload = resolve; image.onerror = reject;
            });
            image.src = URL.createObjectURL(new Blob([
                '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320">' +
                '<rect width="320" height="320" fill="blue"/></svg>'
            ], {type: 'image/svg+xml'}));
            document.querySelector('#reply').append(image);
            await ready;
        }""")

    async def _wait(self, provider, *, pre_count=0, expect_image=False, timeout_ms=240):
        return await provider.wait_for_response_complete(
            self.page, pre_count=pre_count, timeout_ms=timeout_ms,
            expect_image=expect_image, poll_ms=20, stable_polls=2,
        )

    async def test_old_reply_and_image_do_not_satisfy_new_request(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Old answer")
                await self._image()
                await self.page.evaluate("""() => {
                    const user = document.createElement('div');
                    user.dataset.testid = 'user-message';
                    user.innerHTML = '<div class="message-bubble">New request</div>';
                    document.body.append(user);
                }""")
                self.assertFalse(await self._wait(provider, pre_count=1, expect_image=True))

    async def test_stable_text_while_stop_visible_is_not_complete(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Still generating", stop='<button aria-label="Stop response">Stop</button>')
                self.assertFalse(await self._wait(provider))

    async def test_response_settles_after_stream_ends(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Partial", stop='<button aria-label="Stop response">Stop</button>')
                await self.page.evaluate("""() => setTimeout(() => {
                    const reply = document.querySelector('message-content') || document.querySelector('#reply');
                    reply.textContent = 'Final answer';
                    document.querySelector('button').remove();
                }, 150)""")
                self.assertTrue(await self._wait(provider, timeout_ms=700))
                self.assertEqual(await provider.latest_response_text(self.page), "Final answer")

    async def test_image_only_reply_completes_without_text(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider)
                await self._image()
                self.assertTrue(await self._wait(provider, expect_image=True))
                self.assertEqual(await provider.latest_response_text(self.page), "")

    async def test_image_request_does_not_complete_on_text_only(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Creating your image")
                self.assertFalse(await self._wait(provider, expect_image=True))

    async def test_broken_image_with_display_dimensions_is_not_a_result(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Image loading")
                await self.page.evaluate("""() => {
                    const image = new Image(320, 320);
                    image.src = 'https://fixture.invalid/missing.png';
                    document.querySelector('#reply').append(image);
                }""")
                self.assertFalse(await self._wait(provider, expect_image=True))

    async def test_changing_text_at_timeout_is_not_success(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Streaming")
                timer = await self.page.evaluate("""() => setInterval(() => {
                    const reply = document.querySelector('message-content') || document.querySelector('#reply');
                    reply.textContent += '.';
                }, 10)""")
                try:
                    self.assertFalse(await self._wait(provider))
                finally:
                    await self.page.evaluate("timer => clearInterval(timer)", timer)

    async def test_hidden_stop_control_does_not_block_complete_response(self):
        for provider in (gemini, grok):
            with self.subTest(provider=provider.__name__):
                await self._reply(provider, "Done", stop=(
                    '<div style="opacity:0"><button aria-label="Stop response">Stop</button></div>'
                ))
                self.assertTrue(await self._wait(provider))


if __name__ == "__main__":
    unittest.main()
