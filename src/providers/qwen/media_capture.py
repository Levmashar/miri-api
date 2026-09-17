"""Observe this prompt's Qwen completion and asynchronous media task responses.

No extra generation/status requests. Task ids must come from the matching POST,
so a background task from another conversation cannot become this result.
"""

import asyncio
import json
import re
from urllib.parse import unquote, urlparse

from src.providers.qwen.text_capture import _request_contains_prompt


def payloads(body):
    raw = body.decode('utf-8-sig', errors='replace')
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            yield data
        return
    except ValueError:
        pass
    for event in raw.replace('\r\n', '\n').split('\n\n'):
        value = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                yield data
        except ValueError:
            continue


def media_urls(content, kind):
    """Only explicit media content, never an arbitrary recursive URL search."""
    if isinstance(content, str):
        value = content.strip()
        if re.fullmatch(r'https?://[^\s<>"\[\]]+', value):
            return [value]
        if kind == 'image':
            return re.findall(r'!\[[^\]]*\]\((https?://[^\s)]+)\)', value)
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict) and item.get('type') in (kind, kind + '_url'):
                value = item.get(kind + '_url') or item.get('content') or item.get('url')
                if isinstance(value, dict):
                    value = value.get('url')
                result.extend(media_urls(value, kind))
        return result
    return []


class QwenMediaCapture:
    def __init__(self, page, prompt, kind):
        self.page, self.prompt, self.kind = page, prompt, kind
        self.requests, self.tasks, self.task_ids = set(), set(), set()
        self.pending_status = {}
        self.urls = []
        self.error = ''
        self.responses = self.read_errors = 0

    def _request(self, request):
        url = urlparse(request.url)
        if (request.method == 'POST' and url.hostname in ('chat.qwen.ai', 'qwen.ai', 'www.qwen.ai')
                and url.path.rstrip('/').endswith('/chat/completions')
                and _request_contains_prompt(request, self.prompt)):
            self.requests.add(request)

    def _response(self, response):
        url = urlparse(response.url)
        task_id = ''
        if url.hostname in ('chat.qwen.ai', 'qwen.ai', 'www.qwen.ai'):
            match = re.search(r'/task/status/([^/]+)$', url.path)
            task_id = unquote(match[1]) if match else ''
        if response.request not in self.requests and not task_id:
            return
        task = asyncio.create_task(self._read(response, task_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _accept(self, content):
        for url in media_urls(content, self.kind):
            if url not in self.urls:
                self.urls.append(url)

    def _status(self, task_id, data):
        if task_id not in self.task_ids:
            # Status may finish before the completion body is delivered to us.
            if len(self.pending_status) < 32:
                previous = self.pending_status.get(task_id, {})
                if previous.get('task_status') != 'success':
                    self.pending_status[task_id] = data
            return
        status = data.get('task_status', '').lower()
        if status == 'success':
            self._accept(data.get('content'))
        elif status in ('failed', 'failure', 'error', 'cancelled', 'canceled'):
            self.error = 'failed: Qwen media task reported ' + status

    def _completion(self, data):
        data = data.get('data') if isinstance(data.get('data'), dict) else data
        messages = [m for m in data.get('messages', [])
                    if isinstance(m, dict) and m.get('role') == 'assistant']
        # Qwen's non-streaming media reply contains the current assistant at the end.
        if messages:
            messages = messages[-1:]
        for choice in data.get('choices') or []:
            if isinstance(choice, dict):
                message = choice.get('message') or choice.get('delta') or {}
                if isinstance(message, dict):
                    messages.append(dict(message, _finished=choice.get('finish_reason') == 'stop'))
        for message in messages:
            extra = message.get('extra') or {}
            wanx = extra.get('wanx') or {}
            task_id = wanx.get('task_id')
            if task_id:
                self.task_ids.add(str(task_id))
                queued = self.pending_status.pop(str(task_id), None)
                if queued:
                    self._status(str(task_id), queued)
                continue
            phase = message.get('phase', '')
            finished = message.get('done') or message.get('_finished') or message.get('status') in ('finished', 'completed')
            if finished and phase in ('', 'answer', 'final', 'image_gen', 'image_edit'):
                self._accept(message.get('content_list') or message.get('content'))

    async def _read(self, response, task_id):
        try:
            if response.status != 200:
                return
            body = await response.body()
            if len(body) > 16 * 1024 * 1024:
                return
            self.responses += 1
            for data in payloads(body):
                if task_id:
                    data = data.get('data') if isinstance(data.get('data'), dict) else data
                    self._status(task_id, data)
                else:
                    self._completion(data)
        except Exception:
            self.read_errors += 1

    def diagnostics(self):
        return dict(matching_requests=len(self.requests), tasks=len(self.task_ids),
                    responses=self.responses, results=len(self.urls), read_errors=self.read_errors)

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
        await asyncio.gather(*pending, return_exceptions=True)
