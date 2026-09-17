"""Read completed text from the request the Qwen web UI itself sends.

No separate API call or credentials: observe this tab's matching POST only.
DOM extraction remains available for UI/protocol versions with other formats.
"""

import asyncio
import json
from urllib.parse import urlparse


def completed_text(body: bytes, content_type: str) -> str:
    """Qwen/OpenAI SSE (answer phase only), or a JSON chat completion."""
    raw = body.decode('utf-8-sig', errors='replace')
    if 'event-stream' not in content_type and not raw.lstrip().startswith(('data:', 'event:')):
        try:
            data = json.loads(raw)
            choice = data.get('choices', [{}])[0]
            message = choice.get('message', {})
            text = message.get('content', '')
            if isinstance(text, str) and message.get('phase', 'answer') in ('answer', 'final'):
                return text.strip()
        except (ValueError, AttributeError, IndexError, TypeError):
            pass
        return ''

    parts, complete = [], False
    # SSE events can contain several data: lines. Decode complete events rather
    # than transport chunks, which can split Unicode or JSON tokens anywhere.
    for event in raw.replace('\r\n', '\n').split('\n\n'):
        value = '\n'.join(line[5:].lstrip(' ') for line in event.splitlines() if line.startswith('data:')).strip()
        if not value:
            continue
        if value == '[DONE]':
            complete = True
            continue
        try:
            data = json.loads(value)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get('error'):
            return ''
        payload = data.get('data') if isinstance(data.get('data'), dict) else data
        choices = payload.get('choices') or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta') or choice.get('message') or {}
        phase = delta.get('phase') or choice.get('phase') or payload.get('phase') or 'answer'
        if phase not in ('answer', 'final'):
            continue
        content = delta.get('content')
        if isinstance(content, str):
            if 'message' in choice and 'delta' not in choice:
                parts = [content]  # final full snapshot, not another token
            else:
                parts.append(content)
        if choice.get('finish_reason') == 'stop' or delta.get('status') in ('finished', 'completed'):
            complete = True
    return ''.join(parts).strip() if complete else ''


def _request_contains_prompt(request, prompt):
    try:
        data = request.post_data_json
        messages = data.get('messages') or []
        users = [m for m in messages if m.get('role') == 'user']
        content = users[-1].get('content') if users else data.get('prompt')
        if isinstance(content, list):
            content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict))
        norm = lambda s: (s or '').replace('\r\n', '\n').strip()
        return isinstance(content, str) and norm(content) == norm(prompt)
    except (ValueError, TypeError, AttributeError, IndexError):
        return False


class QwenTextCapture:
    def __init__(self, page, prompt):
        self.page, self.prompt = page, prompt
        self.result = asyncio.get_running_loop().create_future()
        self.requests, self.tasks = set(), set()
        self.response_count = 0
        self.formats = set()
        self.parse_failures = 0

    def _request(self, request):
        url = urlparse(request.url)
        if (request.method == 'POST' and url.hostname in ('chat.qwen.ai', 'qwen.ai', 'www.qwen.ai')
                and url.path.rstrip('/').endswith('/chat/completions')
                and _request_contains_prompt(request, self.prompt)):
            self.requests.add(request)

    def _response(self, response):
        if response.request not in self.requests:
            return
        self.response_count += 1
        task = asyncio.create_task(self._read(response))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _read(self, response):
        try:
            content_type = response.headers.get('content-type', '')
            self.formats.add(content_type.split(';')[0])
            if response.status != 200:
                return
            body = await response.body()  # resolves only when the stream closes
            if len(body) > 16 * 1024 * 1024:
                return
            text = completed_text(body, content_type)
            if text and not self.result.done():
                self.result.set_result(text)
        except Exception:
            self.parse_failures += 1

    async def wait(self):
        return await self.result

    def diagnostics(self):
        return {'matching_requests': len(self.requests), 'responses': self.response_count,
                'formats': sorted(self.formats), 'read_errors': self.parse_failures}

    async def __aenter__(self):
        self.page.on('request', self._request)
        self.page.on('response', self._response)
        return self

    async def __aexit__(self, *_):
        self.page.remove_listener('request', self._request)
        self.page.remove_listener('response', self._response)
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not self.result.done():
            self.result.cancel()
