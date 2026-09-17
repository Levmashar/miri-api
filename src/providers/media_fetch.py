"""
Download a generated image or video out of a browser tab and save it to disk.

Generated media reaches the page in one of two forms, and each needs a different
transport:

  * blob: URLs are page-scoped — only code running INSIDE the page can read
    them, so they are fetched in-page (fetch -> arrayBuffer -> base64).
  * http(s) CDN URLs are usually signed and cookie-gated, so a bare urllib fetch
    gets 403. They are pulled with the browser CONTEXT's request API, which
    reuses the session cookies and the account's proxy and is not subject to
    page CORS. If the CDN still refuses, the in-page fetch is tried as well.

This is the transport the Seedream extractor proved out (see
src/providers/seedream/detector.py), lifted into a provider-neutral module so a
new media provider doesn't copy it or import another provider's labelled code.
"""

from __future__ import annotations

import base64
import asyncio
import re
import uuid

from src.core.config import Config

_JS_FETCH_B64 = """
async (url) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
    const credentials = new URL(url, location.href).origin === location.origin ? 'include' : 'omit';
    const resp = await fetch(url, { credentials, signal: controller.signal });
    if (!resp.ok) throw new Error('http ' + resp.status);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return { b64: btoa(binary), type: resp.headers.get('content-type') || '' };
    } finally { clearTimeout(timer); }
}
"""

_IMAGE_EXT = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg",
              "image/jpg": "jpg", "image/gif": "gif", "image/avif": "avif"}
_VIDEO_EXT = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}


async def _in_page(page, src: str) -> "tuple[bytes, str]":
    res = await asyncio.wait_for(page.evaluate(_JS_FETCH_B64, src), timeout=30) or {}
    return base64.b64decode(res.get("b64", "")), res.get("type", "")


async def fetch_bytes(page, src: str, *, log=None, name: str = "provider") -> "tuple[bytes, str]":
    """(data, mime) for a media src, or (b"", "") when every transport failed."""
    if src.startswith(('http://', 'https://')):
        resp = None
        try:
            # Match the page's CDN request, including referer and session cookies.
            resp = await page.context.request.get(src, timeout=30000,
                                                  headers={'Referer': page.url})
            if resp.ok:
                data = await resp.body()
                if _media_mime(data, "image") or _media_mime(data, "video"):
                    return data, (resp.headers or {}).get('content-type', '')
            if log:
                log.warning('%s: media context fetch HTTP %s; trying browser fetch', name, resp.status)
        except Exception as error:
            if log:
                # Exceptions can contain full signed URLs; keep diagnostics safe.
                log.warning('%s: media context fetch %s; trying browser fetch', name, type(error).__name__)
        finally:
            if resp is not None:
                try:
                    await resp.dispose()
                except Exception:
                    pass  # A closing context must not prevent browser fallback.
    try:
        return await _in_page(page, src)
    except Exception as error:
        if log:
            log.warning('%s: browser media fetch failed (%s)', name, type(error).__name__)
        return b'', ''


def _media_mime(data: bytes, kind: str) -> str:
    """Validate bytes: CDN error pages with HTTP 200 are not media files."""
    if kind == 'image':
        if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
        if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
        if data.startswith((b'GIF87a', b'GIF89a')): return 'image/gif'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'image/webp'
        if data[4:8] == b'ftyp' and data[8:12] in (b'avif', b'avis'): return 'image/avif'
    else:
        if data[4:8] == b'ftyp': return 'video/quicktime' if data[8:12] == b'qt  ' else 'video/mp4'
        if data.startswith(b'\x1a\x45\xdf\xa3'): return 'video/webm'
    return ''


def _extension(kind: str, mime: str, src: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    table = _VIDEO_EXT if kind == "video" else _IMAGE_EXT
    if m in table:
        return table[m]
    path = src.split("?", 1)[0].lower()
    for ext in (table.values()):
        if path.endswith("." + ext):
            return ext
    return "mp4" if kind == "video" else "png"


async def download_media(page, src: str, *, kind: str, alt: str = "", log=None,
                         name: str = "provider") -> "tuple[str, str]":
    """Fetch `src` and write it under IMAGES_DIR / VIDEOS_DIR.

    Returns (local_path, mime) — ("", "") when nothing usable came back.
    """
    for attempt in range(3):
        data, mime = await fetch_bytes(page, src, log=log, name=name)
        mime = _media_mime(data, kind)
        if mime:
            break
        if log:
            log.warning('%s: %s download attempt %s/3 returned no valid media (%s bytes)',
                        name, kind, attempt + 1, len(data))
        if attempt < 2:
            await asyncio.sleep(attempt + 1)
    if not mime:
        if log:
            log.warning(f"{name}: could not download generated {kind} after 3 attempts")
        return "", ""

    Config.ensure_dirs()
    ext = _extension(kind, mime, src)
    safe = re.sub(r"[^\w-]", "_", alt)[:40].strip("_") or name.lower()
    base_dir = Config.VIDEOS_DIR if kind == "video" else Config.IMAGES_DIR
    path = base_dir / f"{safe}_{uuid.uuid4().hex[:10]}.{ext}"
    try:
        path.write_bytes(data)
    except Exception as e:
        if log:
            log.error(f"{name}: could not write {kind} file: {e}")
        return "", ""
    if log:
        log.info(f"{name}: saved {kind} ({len(data)} bytes) -> {path.name}")
    resolved_mime = (mime or "").split(";")[0].strip() or (
        "video/mp4" if kind == "video" else f"image/{ext}"
    )
    return str(path), resolved_mime
