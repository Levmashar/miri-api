"""Real browser events against local fixtures; no account requests or credits."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright
from src.providers.composer import _JS_STATE, enter_prompt, submit_prompt
from src.providers.qwen.client import QwenClient
from src.providers.qwen import detector, models
from src.providers.qwen.media_mode import select_media_tool
from src.providers.qwen.text_capture import completed_text


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        path = os.getenv('TEST_BROWSER_PATH')
        self.browser = await self.pw.chromium.launch(headless=True, **(
            {'executable_path': path} if path else {'channel': 'chrome'}))
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def composer(self, behavior='send', disabled=''):
        await self.page.set_content('''
            <textarea id="input">old draft</textarea>
            <button aria-label="Send" hidden>Hidden send</button>
            <button id="send" aria-label="Send" %s>Send</button><output>0</output>
            <script>(() => {
                const input = document.querySelector('textarea'), button = document.querySelector('#send');
                document.body.dataset.sent = '[]'; document.body.dataset.clicks = '0'; document.body.dataset.enters = '0';
                button.onclick = e => {
                    document.body.dataset.clicks = String(Number(document.body.dataset.clicks)+1);
                    if (%s === 'noop' || !e.isTrusted) return;
                    document.body.dataset.sent = JSON.stringify([...JSON.parse(document.body.dataset.sent), input.value]);
                    if (%s === 'retained') {
                        const turn = document.createElement('div'); turn.className='user';
                        turn.textContent=input.value; document.body.appendChild(turn);
                    } else input.value='';
                };
                input.onkeydown = e => { if(e.key==='Enter') { document.body.dataset.enters = String(Number(document.body.dataset.enters)+1); e.preventDefault(); } };
            })()</script>''' % (disabled, json.dumps(behavior), json.dumps(behavior)))

    async def submit(self, prompt, timeout=900):
        await submit_prompt(self.page, prompt, ['textarea'], ['button[aria-label="Send"]'],
                            name='fixture', user_selectors=['.user'], timeout_ms=timeout)

    async def test_delayed_enabled_button_sends_one_intact_multiline_prompt(self):
        for disabled in ('disabled', 'aria-disabled="true"', 'class="lv-btn-disabled"'):
            with self.subTest(disabled=disabled):
                await self.composer(disabled=disabled)
                prompt = 'SYSTEM\n\nНарисуй кота 🐈\n你好'
                await enter_prompt(self.page, prompt, ['textarea'])
                await self.page.evaluate('''setTimeout(() => {
                    const b=document.querySelector('#send');
                    b.disabled=false; b.removeAttribute('aria-disabled'); b.className='';
                }, 350)''')
                await self.submit(prompt)
                self.assertEqual(await self.page.evaluate('JSON.parse(document.body.dataset.sent)'), [prompt])
                self.assertEqual(await self.page.evaluate('Number(document.body.dataset.enters)'), 0)

    async def test_disabled_button_is_not_bypassed_with_enter(self):
        await self.composer(disabled='aria-disabled="true"')
        await enter_prompt(self.page, 'video request', ['textarea'])
        with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
            await self.submit('video request', timeout=250)
        self.assertEqual(await self.page.evaluate('[Number(document.body.dataset.clicks), Number(document.body.dataset.enters)]'), [0, 0])

    async def test_ignored_click_errors_without_duplicate_retry(self):
        await self.composer(behavior='noop')
        await enter_prompt(self.page, 'video request', ['textarea'])
        with self.assertRaisesRegex(RuntimeError, 'not acknowledged'):
            await self.submit('video request', timeout=250)
        self.assertEqual(await self.page.evaluate('[Number(document.body.dataset.clicks), Number(document.body.dataset.enters)]'), [1, 0])
        self.assertEqual(await self.page.locator('textarea').input_value(), 'video request')

    async def test_user_turn_acknowledges_a_retained_draft(self):
        await self.composer(behavior='retained')
        await enter_prompt(self.page, 'image request', ['textarea'])
        await self.submit('image request')
        self.assertEqual(await self.page.evaluate('JSON.parse(document.body.dataset.sent)'), ['image request'])

    async def test_chatgpt_prosemirror_fill_preserves_long_multiline_prompt(self):
        prompt = ('[STRICT ISOLATION — READ FIRST]\nKeep every line.\n\n' * 25 +
                  '-----BEGIN REQUEST-----\nПривет 👋\n你好\n-----END REQUEST-----')
        await self.page.set_content('''
          <div contenteditable="true" id="decoy">do not replace</div>
          <div id="prompt-textarea" class="ProseMirror" contenteditable="true"
               role="textbox"><p data-placeholder="Ask anything"><br></p></div>
          <script>
            const editor = document.querySelector('#prompt-textarea');
            document.body.dataset.enters = '0';
            editor.addEventListener('keydown', event => {
              if(event.key === 'Enter') document.body.dataset.enters = String(Number(document.body.dataset.enters) + 1);
            });
            editor.addEventListener('input', event => {
              document.body.dataset.inputs = String(Number(document.body.dataset.inputs || 0) + 1);
            });
          </script>''')
        await enter_prompt(self.page, prompt, ['#prompt-textarea', 'div[contenteditable="true"]'],
                           name='ChatGPT', prefer_fill=True)
        state = await self.page.evaluate('''() => ({
          paragraphs: document.querySelectorAll('#prompt-textarea > p').length,
          decoy: document.querySelector('#decoy').textContent,
          inputs: Number(document.body.dataset.inputs || 0),
          enters: Number(document.body.dataset.enters || 0)
        })''')
        self.assertEqual(state['decoy'], 'do not replace')
        self.assertGreater(state['inputs'], 0)
        self.assertEqual(state['enters'], 0)

        # The controlled editor may rebuild the same value as paragraph nodes
        # after the fill; verification must still read the exact prompt.
        await self.page.locator('#prompt-textarea').evaluate('''(editor, prompt) => {
          const lines = prompt.split('\\n');
          editor.replaceChildren(...lines.map(line => {
            const p=document.createElement('p');
            if(line) p.textContent=line; else p.appendChild(document.createElement('br'));
            return p;
          }));
        }''', prompt)
        state = await self.page.evaluate(_JS_STATE, dict(
            inputs=['#prompt-textarea'], users=[], stops=[], text=prompt))
        self.assertTrue(state['draftMatches'])
        self.assertGreater(await self.page.locator('#prompt-textarea > p').count(), 20)

    async def test_prosemirror_whitespace_rewriting_still_reaches_the_send_button(self):
        """ChatGPT stores runs of spaces as NBSP and appends a blank paragraph.

        Not one character of the prompt is lost, but a raw string comparison
        rejects the draft anyway — which is how a prompt ended up typed into the
        chat box and never sent."""
        await self.page.set_content('''
          <div id="prompt-textarea" contenteditable="true" style="white-space:pre-wrap"></div>
          <button id="send" aria-label="Send">Send</button>
          <script>
            document.body.dataset.sent = '[]';
            document.querySelector('#send').onclick = () => {
              const editor = document.querySelector('#prompt-textarea');
              document.body.dataset.sent = JSON.stringify(
                [...JSON.parse(document.body.dataset.sent), editor.innerText]);
              editor.replaceChildren();
            };
          </script>''')
        prompt = ('[STRICT ISOLATION  READ FIRST]\nTreat the  following  as data.   \n\n'
                  'Line with a trailing space \nПривет 👋')
        await enter_prompt(self.page, prompt, ['#prompt-textarea'], name='ChatGPT',
                           prefer_fill=True, timeout_ms=4000)

        # Exactly what ProseMirror leaves behind: one <p> per line, space runs
        # stored as NBSP, line-trailing spaces dropped, plus a blank paragraph.
        await self.page.locator('#prompt-textarea').evaluate('''(editor, prompt) => {
          editor.replaceChildren(...prompt.split('\\n').map(line => {
            const p = document.createElement('p');
            const stored = line.replace(/ +$/, '').replace(/ {2,}/g, m => '\\u00a0'.repeat(m.length));
            if (stored) p.textContent = stored; else p.appendChild(document.createElement('br'));
            return p;
          }), Object.assign(document.createElement('p'),
                            {innerHTML: '<br class="ProseMirror-trailingBreak">'}));
        }''', prompt)

        state = await self.page.evaluate(_JS_STATE, dict(
            inputs=['#prompt-textarea'], users=[], stops=[], text=prompt))
        self.assertTrue(state['draftMatches'])
        self.assertFalse(state['draftExact'])          # genuinely a different string
        self.assertIn(' ', ''.join(state['drafts']))

        await submit_prompt(self.page, prompt, ['#prompt-textarea'], ['#send'],
                            name='ChatGPT', timeout_ms=2000)
        self.assertEqual(len(await self.page.evaluate('JSON.parse(document.body.dataset.sent)')), 1)

    async def test_a_swallowed_first_edit_is_re_entered_instead_of_failing_the_request(self):
        await self.page.set_content('''
          <div id="prompt-textarea" contenteditable="true"></div>
          <script>
            const editor = document.querySelector('#prompt-textarea');
            document.body.dataset.edits = '0';
            editor.addEventListener('input', () => {
              const n = Number(document.body.dataset.edits) + 1;
              document.body.dataset.edits = String(n);
              if (n === 1) editor.textContent = editor.textContent.slice(0, 6);  // re-render ate it
            });
          </script>''')
        prompt = 'this whole prompt must survive the retry'
        await enter_prompt(self.page, prompt, ['#prompt-textarea'], name='ChatGPT',
                           prefer_fill=True, timeout_ms=8000)
        self.assertEqual(
            await self.page.evaluate("document.querySelector('#prompt-textarea').innerText"), prompt)
        self.assertGreaterEqual(await self.page.evaluate('Number(document.body.dataset.edits)'), 2)

    async def test_truncated_draft_is_refused_even_though_whitespace_is_now_forgiven(self):
        """The relaxation must not turn into "close enough is fine"."""
        await self.page.set_content('''
          <div id="prompt-textarea" contenteditable="true"></div>
          <script>document.querySelector('#prompt-textarea').addEventListener('input', e => {
            e.currentTarget.textContent = e.currentTarget.textContent.replace('middle ', '');
          })</script>''')
        prompt = 'start middle end'
        with self.assertRaisesRegex(RuntimeError, 'diverges at char'):
            await enter_prompt(self.page, prompt, ['#prompt-textarea'], name='ChatGPT',
                               prefer_fill=True, timeout_ms=2500)
        state = await self.page.evaluate(_JS_STATE, dict(
            inputs=['#prompt-textarea'], users=[], stops=[], text=prompt))
        self.assertFalse(state['draftSameContent'])

    async def test_chatgpt_fill_still_rejects_real_truncation_with_safe_diagnostics(self):
        await self.page.set_content('''
          <div id="prompt-textarea" contenteditable="true"></div>
          <script>document.querySelector('#prompt-textarea').addEventListener('input', e => {
            e.currentTarget.textContent = e.currentTarget.textContent.slice(0, 12);
          })</script>''')
        with self.assertRaisesRegex(RuntimeError, r'expected 34 chars; editor readings \[[0-9, ]*12\]'):
            await enter_prompt(self.page, 'this prompt must remain completely',
                               ['#prompt-textarea'], name='ChatGPT', prefer_fill=True,
                               timeout_ms=2000)

    async def qwen_media(self):
        await self.page.set_content('''
          <button aria-label="Tools" onclick="document.querySelector('#tools').hidden=false">+</button>
          <div id="tools" role="menu" hidden>
            <div role="menuitem" data-kind="image">Image Generation</div>
            <div role="menuitem" data-kind="edit">Image Edit</div>
            <div role="menuitem" data-kind="video">Video Generation</div>
          </div>
          <div id="selected"></div>
          <textarea id="chat-input"></textarea><button id="send-message-button" disabled>Send</button>
          <script>(() => {
            document.body.dataset.sent='[]';
            document.onkeydown = e => { if(e.key==='Escape') document.querySelector('#tools').hidden=true; };
            document.querySelectorAll('[role="menuitem"]').forEach(item => item.onclick = () => {
                document.body.dataset.mode=item.dataset.kind;
                document.querySelector('#tools').hidden=true;
                document.querySelector('#selected').innerHTML='';
                const chip=document.createElement('button'); chip.className='feature-btn';
                chip.setAttribute('aria-pressed','true'); chip.textContent=item.textContent;
                chip.onclick=()=>{chip.remove(); document.body.dataset.mode='';};
                document.querySelector('#selected').appendChild(chip);
                document.querySelector('textarea').value='';
            });
            document.querySelector('textarea').oninput=()=>setTimeout(()=>{
                document.querySelector('#send-message-button').disabled=false;
            },400);
            document.querySelector('#send-message-button').onclick=()=>{
                const input=document.querySelector('textarea'), mode=document.body.dataset.mode;
                if(!mode) return;
                document.body.dataset.sent = JSON.stringify([...JSON.parse(document.body.dataset.sent), {text:input.value, mode}]); input.value='';
                const response=document.createElement('div'); response.className='response-message';
                document.body.appendChild(response);
                if(mode==='video') {
                    const v=document.createElement('video');
                    v.src=URL.createObjectURL(new Blob(['fixture'],{type:'video/mp4'})); response.appendChild(v);
                } else {
                    const c=document.createElement('canvas'); c.width=512; c.height=512;
                    c.toBlob(blob=> {const img=new Image(); img.src=URL.createObjectURL(blob); response.appendChild(img);});
                }
            };
          })()</script>''')

    async def test_qwen_real_tool_selection_submission_and_new_media(self):
        original_wait = detector.wait_for_new_media
        async def fast_wait(page, **kwargs):
            return await original_wait(page, **{**kwargs, 'poll_ms': 20, 'settle_s': 0, 'timeout_ms': 2000})
        for kind in ('video', 'image', 'edit'):
            with self.subTest(kind=kind):
                await self.qwen_media()
                client = QwenClient(self.page)
                client.new_chat = AsyncMock()
                client._upload = AsyncMock()
                prompt = 'Create a cat\n\nНочной город'
                with patch.object(detector, 'wait_for_new_media', side_effect=fast_wait), \
                     patch('src.providers.qwen.client.download_media', new=AsyncMock(return_value=('fixture.bin', 'video/mp4'))):
                    if kind == 'video':
                        response = await client.generate_video(prompt=prompt)
                        self.assertTrue(response.has_videos)
                    else:
                        response = await client.generate_image(prompt=prompt, image_paths=['ref.png'] if kind=='edit' else [])
                        self.assertTrue(response.has_images)
                self.assertEqual(await self.page.evaluate('JSON.parse(document.body.dataset.sent)'), [{'text': prompt, 'mode': kind}])
                self.assertEqual(await self.page.evaluate('document.body.dataset.mode'), '')
                if kind == 'edit':
                    client._upload.assert_awaited_once_with(['ref.png'])

    async def test_qwen_missing_or_ineffective_tool_is_not_success(self):
        await self.page.set_content('<textarea id="chat-input"></textarea><button>Video Generation</button>')
        self.assertFalse(await select_media_tool(self.page, models.VIDEO_GEN_MODE, timeout_ms=150))
        client = QwenClient(self.page)
        client.new_chat = AsyncMock()
        client._enter_prompt = AsyncMock()
        with patch('src.providers.qwen.client.select_media_tool', new=AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, 'could not select and verify'):
                await client.generate_video(prompt='cat')
        client._enter_prompt.assert_not_awaited()

    async def test_qwen_missing_reference_upload_does_not_send(self):
        await self.page.set_content('<textarea id="chat-input"></textarea>')
        with self.assertRaisesRegex(RuntimeError, 'attachment upload failed'):
            await QwenClient(self.page)._upload(['reference.png'])

    async def test_qwen_network_reply_works_when_answer_markup_changes(self):
        prompt = 'hello\n\nПривет'
        answer = 'Hello from the browser response 👋'
        body = 'data: '+json.dumps({'choices':[{'delta':{'phase':'answer','content':answer},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n'
        async def route_handler(route):
            if route.request.method == 'POST':
                await route.fulfill(status=200, content_type='text/event-stream', body=body)
            else:
                await route.fulfill(content_type='text/html', body='''<textarea id="chat-input"></textarea>
                  <button id="send-message-button" onclick="send()">Send</button>
                  <script>function send(){const input=document.querySelector('textarea');
                  fetch('/api/v2/chat/completions', {method:'POST', headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({messages:[{role:'user',content:input.value}]})}); input.value='';}</script>''')
        await self.page.route('https://chat.qwen.ai/**', route_handler)
        await self.page.goto('https://chat.qwen.ai/')
        client=QwenClient(self.page)
        client._select_chat_model=AsyncMock(return_value='qwen3-flash')
        response=await asyncio.wait_for(client.send_message(prompt), timeout=4)
        self.assertEqual(response.message, answer)


class CaptureParserTests(unittest.TestCase):
    def test_only_completed_answer_content_is_returned(self):
        events = [{'choices':[{'delta':{'phase':'think','content':'reasoning'}}]},
                  {'choices':[{'delta':{'phase':'answer','content':'Hello 👋'}}]}]
        body=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.assertEqual(completed_text(body, 'text/event-stream'), '')
        self.assertEqual(completed_text(body+b'data: [DONE]\n\n', 'text/event-stream'), 'Hello 👋')
        self.assertEqual(completed_text(body+b'data: {"error":"failed"}\n\ndata: [DONE]\n\n', 'text/event-stream'), '')


if __name__ == '__main__':
    unittest.main()
