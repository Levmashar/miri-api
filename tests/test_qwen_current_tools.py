"""Fixtures based on Qwen's public qwen-chat-fe/0.2.91 mode-selector markup."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright
from src.providers.qwen.client import QwenClient
from src.providers.qwen import models, detector
from src.providers.qwen.media_mode import select_media_tool, clear_media_tool


class CurrentToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv('TEST_BROWSER_PATH')
        self.browser = await self.pw.chromium.launch(headless=True, **(
            {'executable_path': path} if path else {'channel': 'chrome'}))
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def composer(self, *, delayed=False, disabled=False, ignore_click=False):
        await self.page.set_content('''
          <div class="message-input-container">
            <div class="mode-select">
              <div class="mode-select-open" role="button" aria-label="Select Mode">+</div>
              <div id="current"></div>
            </div>
            <textarea id="chat-input"></textarea>
            <div class="chat-prompt-send-button"><button class="send-button" disabled>Send</button></div>
          </div>
          <div class="qwen-chat-v2-dropdown-menu-popup" hidden>
            <div class="mode-select-dropdown-menu">
              <div class="mode-select-common-item" data-mode="image">
                <span class="mode-select-dropdown-item-name">Create image<span class="mode-select-dropdown-item-tag">New</span></span>
              </div>
              <div class="mode-select-common-item" data-mode="video">
                <span class="mode-select-dropdown-item-name">Create video</span>
              </div>
            </div>
          </div>
          <script>(()=>{
            const popup=document.querySelector('.qwen-chat-v2-dropdown-menu-popup');
            const input=document.querySelector('textarea'), send=document.querySelector('.send-button');
            document.body.dataset.sent='[]';document.body.dataset.toolClicks='0';
            document.querySelector('.mode-select-open').onclick=()=>{popup.hidden=!popup.hidden};
            document.onkeydown=e=>{if(e.key==='Escape')popup.hidden=true};
            document.querySelectorAll('.mode-select-common-item').forEach(item=>item.onclick=event=>{
              if(!event.isTrusted || item.getAttribute('aria-disabled')==='true')return;
              document.body.dataset.toolClicks=String(Number(document.body.dataset.toolClicks)+1);
              if(document.body.dataset.ignore==='true')return;
              document.body.dataset.mode=item.dataset.mode;popup.hidden=true;
              const mode=document.createElement('div');mode.className='mode-select-current-mode';
              mode.setAttribute('aria-disabled','false');
              mode.innerHTML='<span>Create '+item.dataset.mode+'</span><i class="mode-select-current-mode-close" style="display:inline-block;width:20px;height:20px">×</i>';
              mode.querySelector('i').onclick=()=>{mode.remove();document.body.dataset.mode=''};
              document.querySelector('#current').replaceChildren(mode);
            });
            input.oninput=()=>setTimeout(()=>{send.disabled=false},200);
            send.onclick=()=>{
              if(!document.body.dataset.mode)return;
              document.body.dataset.sent=JSON.stringify([{mode:document.body.dataset.mode,text:input.value}]);input.value='';
            };
          })()</script>''')
        if delayed:
            await self.page.evaluate('''const trigger=document.querySelector('.mode-select-open');trigger.hidden=true;
                setTimeout(()=>{trigger.hidden=false},500)''')
        if disabled:
            await self.page.locator('[data-mode="video"]').evaluate("e=>{e.setAttribute('aria-disabled','true');e.classList.add('qwen-chat-v2-dropdown-item-disabled')}")
        if ignore_click:
            await self.page.evaluate("document.body.dataset.ignore='true'")

    async def test_actual_custom_menu_and_removable_mode_for_image_and_video(self):
        for labels, kind in [(models.IMAGE_GEN_MODE, 'image'), (models.VIDEO_GEN_MODE, 'video')]:
            await self.composer(delayed=True)
            self.assertTrue(await select_media_tool(self.page, labels, timeout_ms=2500))
            self.assertEqual(await self.page.evaluate('document.body.dataset.mode'), kind)
            self.assertTrue(await select_media_tool(self.page, labels, timeout_ms=500))
            self.assertEqual(await self.page.evaluate('Number(document.body.dataset.toolClicks)'), 1)
            await clear_media_tool(self.page)
            self.assertEqual(await self.page.locator('.mode-select-current-mode').count(), 0)

    async def test_custom_tool_pipeline_sends_the_full_prompt_once_and_cleans_up(self):
        prompt = 'Create a cat\n\nНочной город 🐈'
        for kind in ('image', 'video'):
            await self.composer()
            client = QwenClient(self.page)
            client.new_chat = AsyncMock()
            with patch.object(detector, 'wait_for_new_media', new=AsyncMock(return_value=['https://media.test/result'])), \
                 patch('src.providers.qwen.client.download_media', new=AsyncMock(return_value=('fixture.bin', 'video/mp4'))):
                if kind == 'image':
                    result = await client.generate_image(prompt=prompt)
                    self.assertTrue(result.has_images)
                else:
                    result = await client.generate_video(prompt=prompt)
                    self.assertTrue(result.has_videos)
            self.assertEqual(await self.page.evaluate('JSON.parse(document.body.dataset.sent)'), [{'mode':kind, 'text':prompt}])
            self.assertEqual(await self.page.locator('.mode-select-current-mode').count(), 0)

    async def test_disabled_and_ignored_tools_never_report_success(self):
        for disabled, ignore in [(True, False), (False, True)]:
            await self.composer(disabled=disabled, ignore_click=ignore)
            self.assertFalse(await select_media_tool(self.page, models.VIDEO_GEN_MODE, timeout_ms=800))
            self.assertEqual(await self.page.locator('textarea').input_value(), '')
            self.assertEqual(await self.page.evaluate('Number(document.body.dataset.toolClicks)'), 0 if disabled else 1)

    async def test_old_menu_row_covered_by_overlay_does_not_prevent_other_visible_row(self):
        await self.page.set_content('''<div style="position:relative;width:160px;height:45px">
            <button>Video Generation</button><div style="position:absolute;inset:0;background:white"></div></div>
            <button onclick="this.setAttribute('aria-pressed','true')">Video Generation</button>''')
        self.assertTrue(await select_media_tool(self.page, models.VIDEO_GEN_MODE, timeout_ms=1000))

    async def test_plain_text_request_clears_previous_media_tool(self):
        await self.composer()
        self.assertTrue(await select_media_tool(self.page, models.VIDEO_GEN_MODE, timeout_ms=1500))
        client = QwenClient(self.page)
        with patch('src.providers.text_client.TextChatClient.send_message', new=AsyncMock()) as send:
            await client.send_message('hi')
        send.assert_awaited_once()
        self.assertEqual(await self.page.locator('.mode-select-current-mode').count(), 0)
