"""
Request/response log for the admin **Logs** viewer.

A bounded, optionally persistent buffer of the last N API requests, each with its full
(size-capped) request and response body. This is a live DEBUGGING aid, not an
audit log: it is admin-only, persists redacted records when a database path is configured.

Capture is a pure-ASGI middleware (NOT BaseHTTPMiddleware — that trips the
Python 3.9 event-loop bug this codebase avoids elsewhere). It records only real
API traffic (/v1/*, legacy chat) — never /admin, /docs, /healthz — so the Logs
UI polling the control plane can't feed back on itself.

Memory safety: multipart uploads and large bodies are NOT buffered (only a note
is stored), so a 25 MB image edit can't be duplicated in RAM; and each stored
body is capped (BODY_CAP) with the true byte size recorded for honesty.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import sqlite3
from pathlib import Path
import time
from collections import OrderedDict

from src.providers.base import ENABLED_PROVIDER_IDS, model_id_to_provider

# Content types we render as text in the viewer. Anything else is stored as a
# short placeholder with its byte size rather than mojibake.
_TEXTUAL = ("application/json", "text/", "application/x-www-form-urlencoded",
            "application/xml", "text/event-stream")

# Headers never stored (they carry the API key / session).
_REDACT_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}

# ── Media stripping ─────────────────────────────────────────────
# We never store the ACTUAL bytes of generated/uploaded media in the log — only
# a size note. Two shapes are stripped from stored bodies:
#   1. base64 data URLs: data:image/png;base64,<payload>   (vision inputs, frames)
#   2. bare long base64 runs: "b64_json":"<payload>", image_generation result…
# Markers are quote/brace-free so the surrounding JSON stays valid (and still
# pretty-prints in the viewer).
_DATA_URL_RE = re.compile(
    r"(data:(?:image|video|audio|application)/[a-zA-Z0-9.+-]+;base64,)([A-Za-z0-9+/=]+)"
)
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def _strip_media(text: str) -> str:
    """Replace embedded base64 media payloads with a short size note."""
    if not text:
        return text

    def _du(m):
        approx = (len(m.group(2)) * 3) // 4
        return f"{m.group(1)}<media stripped ~{approx} bytes>"

    text = _DATA_URL_RE.sub(_du, text)
    text = _B64_RUN_RE.sub(lambda m: f"<base64 {len(m.group(0))} chars stripped>", text)
    return text


def _is_textual(ct: str) -> bool:
    ct = (ct or "").lower()
    return any(ct.startswith(t) or t in ct for t in _TEXTUAL)


def _headers_to_dict(headers, *, redact: bool = False) -> dict:
    """Normalise an ASGI header list [(b'k', b'v'), ...] (or a dict) to str->str."""
    out: dict[str, str] = {}
    if isinstance(headers, dict):
        items = headers.items()
    else:
        items = (
            ((k.decode("latin-1"), v.decode("latin-1")) if isinstance(k, (bytes, bytearray)) else (k, v))
            for (k, v) in (headers or [])
        )
    for k, v in items:
        lk = str(k).lower()
        if redact and lk in _REDACT_HEADERS:
            out[lk] = "***redacted***"
        else:
            out[lk] = str(v)
    return out


def _header_value(headers, name: str) -> str:
    name = name.lower()
    for k, v in (headers or []):
        kk = k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else k
        if str(kk).lower() == name:
            return v.decode("latin-1") if isinstance(v, (bytes, bytearray)) else str(v)
    return ""


def _parse_model_provider(path: str, body_text: str, ct: str) -> "tuple[str, str]":
    """Best-effort (model, provider) for the summary row.

    Provider comes from an explicit /v1/<provider>/... prefix, else from mapping
    the request's `model`. Model comes from the JSON body's `model` field."""
    provider = ""
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "v1" and parts[1] in ENABLED_PROVIDER_IDS:
        provider = parts[1]

    if parts and parts[0] == "temporary":
        return _parse_model_provider("/" + "/".join(parts[1:]), body_text, ct)

    model = ""
    if body_text and ct and "json" in ct.lower():
        try:
            j = json.loads(body_text)
            if isinstance(j, dict):
                model = j.get("model") or ""
                if not isinstance(model, str):
                    model = ''
        except Exception:
            pass
    if not provider and model:
        provider = model_id_to_provider(model) or ""
    return model, provider


class RequestLogStore:
    """Bounded ring buffer of request/response records."""

    def __init__(self, retention_seconds: float = 7 * 86400,
                 max_entries: int = 50000, body_cap: int = 256 * 1024, db_path=None) -> None:
        self._d: "OrderedDict[int, dict]" = OrderedDict()
        self._retention = retention_seconds  # prune entries older than this
        self._max = max_entries              # hard count backstop (memory guard)
        self.body_cap = body_cap             # per-body char cap AFTER stripping
        self._seq = 0
        self._lock = asyncio.Lock()
        self._db = None
        self.persistence_error = ''
        if db_path is not None:
            try:
                # Single gateway process; small, bounded transactions with WAL.
                #
                # check_same_thread=False because the connection is opened by
                # whichever thread imports this module while every write comes
                # from the serving thread. When those differ (uvicorn --reload,
                # an ASGI test client, anything that imports the app off the
                # loop thread) sqlite3 raises ProgrammingError on EVERY write:
                # the viewer silently drops to memory-only and the history
                # vanishes at the next restart. Writes are already serialised by
                # self._lock, so sharing the connection is safe.
                path = Path(db_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(path), timeout=0.2, check_same_thread=False)
                self._db.execute('PRAGMA journal_mode=WAL')
                self._db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, ts REAL, entry TEXT)')
                self._db.execute('CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts)')
                self._seq = self._db.execute('SELECT COALESCE(MAX(id), 0) FROM requests').fetchone()[0]
                rows = self._db.execute('SELECT entry FROM requests WHERE ts >= ? ORDER BY id DESC LIMIT ?',
                                        (time.time() - self._retention, self._max)).fetchall()
                for (raw,) in reversed(rows):
                    entry = json.loads(raw)
                    if entry.get('state') == 'in_progress':
                        entry.update(state='interrupted', status=503,
                                     error='Gateway restarted before the request completed')
                    self._d[entry['id']] = entry
                self._persist()
            except (OSError, sqlite3.Error, ValueError) as error:
                if self._db is not None:
                    self._db.close()
                    self._db = None
                self.persistence_error = 'Request log database unavailable; new records are only in memory'
                logging.getLogger('api_server').error('%s (%s)', self.persistence_error, type(error).__name__)

    def _persist(self, entry=None):
        if self._db is None:
            return
        try:
            with self._db:
                if self.persistence_error and self._d:
                    self._db.executemany('INSERT OR REPLACE INTO requests VALUES (?, ?, ?)',
                        [(e['id'], e['ts'], json.dumps(e, ensure_ascii=False)) for e in self._d.values()])
                elif entry is not None:
                    self._db.execute('INSERT OR REPLACE INTO requests VALUES (?, ?, ?)',
                                     (entry['id'], entry['ts'], json.dumps(entry, ensure_ascii=False)))
                elif self._d:
                    self._db.executemany('INSERT OR REPLACE INTO requests VALUES (?, ?, ?)',
                        [(e['id'], e['ts'], json.dumps(e, ensure_ascii=False)) for e in self._d.values()])
                self._db.execute('DELETE FROM requests WHERE ts < ?', (time.time() - self._retention,))
                self._db.execute('DELETE FROM requests WHERE id < (SELECT id FROM requests ORDER BY id DESC LIMIT 1 OFFSET ?)', (max(0, self._max - 1),))
            self.persistence_error = ''
        except sqlite3.Error as error:
            self.persistence_error = 'Request log disk write failed; recent entries are only in memory'
            logging.getLogger('api_server').error('%s (%s)', self.persistence_error, type(error).__name__)

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def _text(self, raw: bytes, true_size: int, content_type: str, *, captured: bool):
        """(text, truncated, size, shown). Strips base64 media before storing."""
        if not captured:
            return (f"<{content_type or 'body'} · {true_size} bytes — not captured "
                    "(multipart or large upload)>", False, true_size, False)
        if true_size == 0:
            return "", False, 0, True
        if not _is_textual(content_type):
            return (f"<{content_type or 'binary'} · {true_size} bytes — not shown>",
                    False, true_size, False)
        # The middleware bounds how much it buffers, so we may not have every byte.
        truncated = true_size > len(raw)
        try:
            text = raw.decode("utf-8", "replace")
        except Exception:
            return f"<{true_size} bytes — undecodable>", truncated, true_size, False
        text = _strip_media(text)  # NEVER store the actual image/media bytes
        if len(text) > self.body_cap:
            text = text[: self.body_cap]
            truncated = True
        return text, truncated, true_size, True

    async def add(self, *, method, path, status, duration_ms, req_headers, req_body,
                  req_size, req_captured, resp_headers, resp_body, resp_size,
                  client, streaming, eid=None, state='completed', error='') -> int:
        req_ct = _header_value(req_headers, "content-type")
        resp_ct = _header_value(resp_headers, "content-type")
        req_text, req_trunc, req_size, req_shown = self._text(
            req_body, req_size, req_ct, captured=req_captured)
        resp_text, resp_trunc, resp_size, resp_shown = self._text(
            resp_body, resp_size, resp_ct, captured=True)
        model, provider = _parse_model_provider(path, req_text if req_shown else "", req_ct)

        async with self._lock:
            if eid is None:
                self._seq += 1
                eid = self._seq
            now = self._d.get(eid, {}).get('ts', time.time())
            self._d[eid] = {
                "id": eid,
                "state": state,
                "error": error,
                "ts": now,
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round(duration_ms, 1),
                "provider": provider,
                "model": model,
                "client": client,
                "streaming": bool(streaming),
                "req_content_type": req_ct,
                "resp_content_type": resp_ct,
                "req_size": req_size,
                "resp_size": resp_size,
                "req_truncated": req_trunc,
                "resp_truncated": resp_trunc,
                "req_headers": _headers_to_dict(req_headers, redact=True),
                "resp_headers": _headers_to_dict(resp_headers, redact=True),
                "req_body": req_text,
                "resp_body": resp_text,
            }
            # Prune by AGE (keep ~a week); entries are inserted chronologically,
            # so the oldest are at the front. A hard count is only a memory guard.
            cutoff = time.time() - self._retention
            while self._d:
                oldest = next(iter(self._d))
                if self._d[oldest]["ts"] < cutoff:
                    self._d.popitem(last=False)
                else:
                    break
            while len(self._d) > self._max:
                self._d.popitem(last=False)
            self._persist(self._d.get(eid))
        return eid

    _SUMMARY_KEYS = ("id", "ts", "method", "path", "status", "duration_ms",
                     "provider", "model", "req_size", "resp_size", "streaming", "state", "error")

    def page(self, limit=200, *, before=None, query=''):
        query = query.strip().lower()
        matches = []
        for entry in reversed(self._d.values()):
            if query and query not in ' '.join(str(entry.get(k, '')) for k in
                    ('path', 'model', 'provider', 'method', 'status', 'state')).lower():
                continue
            matches.append(entry)
        selected = [e for e in matches if before is None or e['id'] < before][:limit + 1]
        more = len(selected) > limit
        selected = selected[:limit]
        rows = [{k: e.get(k, '') for k in self._SUMMARY_KEYS} for e in selected]
        for row in rows:
            if row['state'] == 'in_progress':
                row['duration_ms'] = round((time.time() - row['ts']) * 1000, 1)
        return dict(count=len(self._d), matched=len(matches), entries=rows,
                    next_before=rows[-1]['id'] if more else None,
                    persistent=self._db is not None, warning=self.persistence_error)

    def summaries(self, limit: int = 200) -> list:
        return self.page(limit or len(self._d))['entries']

    def export(self, limit: int = 200, *, query: str = '') -> list:
        """The last `limit` entries IN FULL (headers + bodies), newest first.

        `page()` deliberately returns summaries — enough to draw a table row.
        Debugging a failure needs the body that came back, and clicking into
        each entry one at a time does not survive contact with a real incident,
        so the download hands over whole records.
        """
        query = query.strip().lower()
        out = []
        for entry in reversed(self._d.values()):
            if query and query not in ' '.join(str(entry.get(k, '')) for k in
                    ('path', 'model', 'provider', 'method', 'status', 'state')).lower():
                continue
            out.append(dict(entry))
            if len(out) >= max(1, limit):
                break
        return out

    def get(self, eid: int) -> "dict | None":
        return self._d.get(eid)

    def count(self) -> int:
        return len(self._d)

    async def clear(self) -> None:
        async with self._lock:
            self._d.clear()
            if self._db is not None:
                with self._db:
                    self._db.execute('DELETE FROM requests')


class RequestLogMiddleware:
    """Pure-ASGI middleware that records API requests into a RequestLogStore."""

    SKIP_PREFIXES = ("/admin", "/docs", "/redoc", "/openapi.json", "/healthz", "/favicon")
    # Above this, or for multipart, the request body is NOT buffered (memory).
    MAX_CAPTURE_REQ = 1 * 1024 * 1024
    # Response body buffered up to here (bounds memory). Base64 media is stripped
    # afterwards, so this budget mostly holds useful text, not image bytes.
    MAX_CAPTURE_RESP = 1 * 1024 * 1024

    def __init__(self, app, store: RequestLogStore) -> None:
        self.app = app
        self.store = store

    def _should_log(self, path: str) -> bool:
        return not any(path == p or path.startswith(p + "/") or path.startswith(p)
                       for p in self.SKIP_PREFIXES)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self._should_log(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        req_ct = _header_value(headers, "content-type")
        try:
            content_length = int(_header_value(headers, "content-length") or 0)
        except ValueError:
            content_length = 0
        # Skip buffering multipart / large uploads — capture only a note for them.
        capture_req = not req_ct.lower().startswith("multipart/") and content_length <= self.MAX_CAPTURE_REQ

        req_cap, resp_cap = self.MAX_CAPTURE_REQ, self.MAX_CAPTURE_RESP
        req_size = 0
        req_buf, resp_buf = bytearray(), bytearray()
        state = {'status': 0, 'headers': [], 'size': 0, 'streaming': False, 'complete': False}
        start = time.monotonic()
        eid = None
        disconnected = False

        async def record(phase, error=''):
            nonlocal eid
            try:
                client = scope.get('client')
                eid = await self.store.add(
                    eid=eid, state=phase, error=error,
                    method=scope.get('method', ''), path=scope.get('path', ''),
                    status=state['status'] or (499 if phase == 'interrupted' else 500 if phase == 'failed' else 0),
                    duration_ms=(time.monotonic() - start) * 1000,
                    req_headers=headers, req_body=bytes(req_buf), req_size=req_size,
                    req_captured=capture_req, resp_headers=state['headers'],
                    resp_body=bytes(resp_buf), resp_size=state['size'],
                    client=client[0] if client else '', streaming=state['streaming'])
            except Exception as exc:
                logging.getLogger('api_server').error('Could not record API request (%s)', type(exc).__name__)

        async def recv():
            nonlocal req_size, disconnected
            message = await receive()
            if message['type'] == 'http.request':
                body = message.get('body', b'')
                req_size += len(body)
                if capture_req and len(req_buf) < req_cap:
                    req_buf.extend(body[:req_cap - len(req_buf)])
                if not message.get('more_body', False):
                    await record('in_progress')
            elif message['type'] == 'http.disconnect':
                disconnected = True
            return message

        async def capture(message):
            if message['type'] == 'http.response.start':
                state['status'] = message['status']
                state['headers'] = message.get('headers', [])
                # A served image/video is stored as a size note either way, so
                # buffering a megabyte of it per fetch buys nothing. The entry
                # itself is still recorded — seeing that a client fetched a
                # generated file, and what it got back, is the point.
                state['capture_body'] = _is_textual(_header_value(state['headers'], 'content-type'))
            elif message['type'] == 'http.response.body':
                chunk = message.get('body', b'')
                state['size'] += len(chunk)
                if state.get('capture_body', True) and len(resp_buf) < resp_cap:
                    resp_buf.extend(chunk[:resp_cap - len(resp_buf)])
                if message.get('more_body', False):
                    state['streaming'] = True
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                state['complete'] = True

        await record('in_progress')
        failure = ''
        try:
            await self.app(scope, recv, capture)
        except BaseException as exc:
            failure = type(exc).__name__
            raise
        finally:
            phase = 'completed' if state['complete'] else 'interrupted' if disconnected or failure == 'CancelledError' else 'failed'
            await record(phase, failure)
