"""
Seedream / Dreamina response-completion detector + video extractor.

Completion is detected by DOM DIFFING, not by parsing a progress bar (this is how
the reference 10x-chat driver does it, and it is robust):

  1. Snapshot the <video> srcs already on the page BEFORE clicking generate.
  2. Poll every ~3s for a NEW <video> inside [class*="responsive-video-grid"].
  3. Ignore the loading placeholder (src on capcutstatic.com — a spinner mp4).
  4. Accept when the new src is a blob: URL OR its path contains /video/.
  5. Bail early if the page shows failure text.

Download handles both delivery forms the UI uses:
  - blob:  -> fetched IN-PAGE via fetch()->arrayBuffer, base64'd, decoded to disk.
  - https: -> pulled with the browser context's request API (reuses cookies +
             proxy, and is not subject to page CORS), decoded to disk.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid

from patchright.async_api import Page

from src.core.config import Config
from src.core.log import setup_logging
from src.providers.seedream.selectors import (
    ACCEPT_VIDEO_PATH_RE,
    CREDIT_LIMIT_TEXT_RE,
    ERROR_SURFACE,
    EXPECTED_IMAGES,
    FAILURE_TEXT_RE,
    MIN_IMAGE_DIM,
    SPINNER_SRC_RE,
    SeedreamSelectors as S,
)

log = setup_logging("seedream_detector")

_SPINNER = re.compile(SPINNER_SRC_RE, re.I)
_ACCEPT = re.compile(ACCEPT_VIDEO_PATH_RE, re.I)
_FAILURE = re.compile(FAILURE_TEXT_RE, re.I)
_CREDIT = re.compile(CREDIT_LIMIT_TEXT_RE, re.I)

# Read every <video> src currently in the result grid (blob or http).
_JS_VIDEO_SRCS = """
() => {
    const vids = Array.from(document.querySelectorAll(
        "[class*='responsive-video-grid'] video, [class*='responsive-video-grid'] source"));
    const out = [];
    for (const v of vids) {
        const src = v.currentSrc || v.src || (v.querySelector && v.querySelector('source') ? v.querySelector('source').src : '') || '';
        if (src) out.push(src);
    }
    return out;
}
"""


def _is_result_src(src: str) -> bool:
    """A finished video src: blob: URL or a CDN mp4 with /video/ in the path."""
    if not src or _SPINNER.search(src):
        return False
    return src.startswith("blob:") or bool(_ACCEPT.search(src))


async def capture_video_srcs(page: Page) -> set:
    """Baseline snapshot of result-grid video srcs before generating."""
    try:
        srcs = await page.evaluate(_JS_VIDEO_SRCS) or []
        return {s for s in srcs if s}
    except Exception:
        return set()


# Read text ONLY from transient alert/toast/dialog surfaces, never the whole
# page — persistent nav/credit/upgrade chrome must not be mistaken for an error.
_JS_ERROR_TEXT = """
(sel) => {
    const parts = [];
    document.querySelectorAll(sel).forEach(el => {
        const t = (el.innerText || el.textContent || '').trim();
        if (t) parts.push(t);
    });
    return parts.join('\\n');
}
"""


async def _error_text(page: Page) -> str:
    """innerText of visible alert/toast/dialog surfaces only (see ERROR_SURFACE)."""
    try:
        return (await page.evaluate(_JS_ERROR_TEXT, ERROR_SURFACE)) or ""
    except Exception:
        return ""


async def wait_for_new_video(
    page: Page,
    *,
    baseline: set,
    timeout_ms: int,
    poll_ms: int = 3000,
) -> "str | None":
    """Poll until a NEW finished video appears, or failure/timeout.

    Returns the accepted src on success, None on timeout. Raises RuntimeError
    with a "credit:"/"failed:" prefix when the page reports a paywall/failure, so
    the client can surface a limit vs a plain failure."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    log.info(f"Seedream: waiting for video (timeout {timeout_ms}ms, {len(baseline)} baseline)")
    while time.monotonic() < deadline:
        try:
            srcs = await page.evaluate(_JS_VIDEO_SRCS) or []
        except Exception:
            srcs = []
        for src in srcs:
            if src and src not in baseline and _is_result_src(src):
                log.info(f"Seedream: new video ready ({src[:60]})")
                return src

        # Only scan transient alert/toast/dialog surfaces — never the whole page
        # (persistent credit/upgrade chrome would false-positive on poll #1 and
        # take the account offline mid-generation).
        text = await _error_text(page)
        if text:
            if _CREDIT.search(text):
                raise RuntimeError("credit: Dreamina reports insufficient credits")
            if _FAILURE.search(text):
                snippet = text[:200].replace("\n", " ")
                raise RuntimeError(f"failed: {snippet}")

        await asyncio.sleep(poll_ms / 1000)

    log.warning("Seedream: no finished video before timeout")
    return None


# In-page: fetch a (blob: or same-origin) URL and return base64 + mime type.
_JS_FETCH_B64 = """
async (url) => {
    const resp = await fetch(url, { credentials: 'include' });
    if (!resp.ok) throw new Error('http ' + resp.status);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    }
    return { b64: btoa(binary), type: resp.headers.get('content-type') || '' };
}
"""


def _ext_for(mime: str, src: str) -> str:
    m = (mime or "").lower()
    if "webm" in m or src.lower().endswith(".webm"):
        return "webm"
    if "quicktime" in m or src.lower().endswith(".mov"):
        return "mov"
    return "mp4"


async def extract_video(page: Page, src: str, *, alt: str = "") -> "str | None":
    """Download the finished video to VIDEOS_DIR. Returns the local path or None.

    blob: URLs can only be read from inside the page (they are page-scoped), so
    those go through an in-page fetch. http(s) CDN URLs are pulled with the
    browser context's request API, which reuses the session cookies and proxy and
    sidesteps page CORS."""
    Config.ensure_dirs()
    data: bytes
    mime = ""
    try:
        if src.startswith("blob:"):
            res = await page.evaluate(_JS_FETCH_B64, src)
            data = base64.b64decode(res.get("b64", ""))
            mime = res.get("type", "")
        else:
            # Reuse the context (cookies + proxy); not subject to page CORS.
            resp = await page.context.request.get(src)
            if not resp.ok:
                log.warning(f"Seedream: context fetch failed ({resp.status}) — trying in-page")
                res = await page.evaluate(_JS_FETCH_B64, src)
                data = base64.b64decode(res.get("b64", ""))
                mime = res.get("type", "")
            else:
                data = await resp.body()
                mime = (resp.headers or {}).get("content-type", "")
    except Exception as e:
        log.error(f"Seedream: video download failed: {e}")
        return None

    if not data:
        log.warning("Seedream: downloaded video was empty")
        return None

    ext = _ext_for(mime, src)
    safe = re.sub(r"[^\w-]", "_", alt)[:40].strip("_") or "seedream"
    fn = f"{safe}_{uuid.uuid4().hex[:10]}.{ext}"
    path = Config.VIDEOS_DIR / fn
    try:
        path.write_bytes(data)
    except Exception as e:
        log.error(f"Seedream: could not write video file: {e}")
        return None
    log.info(f"Seedream: saved video ({len(data)} bytes) -> {path.name}")
    return str(path)


# ── Image generation (Dreamina image tab) ───────────────────────
# Reads finished result-grid <img> elements. Dreamina streams up to 4 variants
# per text→image request from a signed ByteDance ImageX CDN.
_JS_IMAGES = """
(sel) => {
    const out = [];
    document.querySelectorAll(sel).forEach(im => {
        const src = im.currentSrc || im.src || '';
        if (src) out.push({ src, w: im.naturalWidth || 0, complete: !!im.complete });
    });
    return out;
}
"""

_IMG_EXT = {"image/webp": "webp", "image/png": "png", "image/jpeg": "jpg",
            "image/jpg": "jpg", "image/gif": "gif"}


def _is_result_image(src: str, w, complete) -> bool:
    """A finished result image: loaded, big enough, not a placeholder."""
    if not src or src.startswith("data:") or _SPINNER.search(src):
        return False
    if not complete or (w or 0) <= MIN_IMAGE_DIM:
        return False
    return src.startswith("blob:") or src.startswith("http")


async def capture_image_srcs(page: Page) -> set:
    """Baseline snapshot of result-grid <img> srcs before generating."""
    try:
        items = await page.evaluate(_JS_IMAGES, S.RESULT_IMAGE) or []
        return {it.get("src", "") for it in items if it.get("src")}
    except Exception:
        return set()


async def wait_for_new_images(
    page: Page,
    *,
    baseline: set,
    timeout_ms: int,
    min_count: int = 1,
    poll_ms: int = 2500,
    settle_s: float = 8.0,
) -> list:
    """Poll until NEW finished images appear, then briefly settle to collect the
    full set (Dreamina streams ~4). Returns the accepted src list ([] on timeout).

    Raises RuntimeError with a 'credit:'/'failed:' prefix on a paywall/failure."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    found: dict = {}
    settle_until = None
    log.info(f"Seedream: waiting for image(s) (timeout {timeout_ms}ms, {len(baseline)} baseline)")
    while time.monotonic() < deadline:
        try:
            items = await page.evaluate(_JS_IMAGES, S.RESULT_IMAGE) or []
        except Exception:
            items = []
        for it in items:
            src = it.get("src", "")
            if src and src not in baseline and _is_result_image(src, it.get("w"), it.get("complete")):
                found[src] = True

        text = await _error_text(page)
        if text:
            if _CREDIT.search(text):
                raise RuntimeError("credit: Dreamina reports insufficient credits")
            if _FAILURE.search(text):
                raise RuntimeError(f"failed: {text[:200]}".replace("\n", " "))

        if found:
            if len(found) >= EXPECTED_IMAGES:
                break
            now = time.monotonic()
            if settle_until is None:
                settle_until = now + settle_s
            elif now >= settle_until and len(found) >= min_count:
                break
        await asyncio.sleep(poll_ms / 1000)

    srcs = list(found)
    if srcs:
        log.info(f"Seedream: {len(srcs)} image(s) ready")
    else:
        log.warning("Seedream: no finished image before timeout")
    return srcs


async def extract_image(page: Page, src: str, *, alt: str = "") -> "str | None":
    """Download a finished result image to IMAGES_DIR. Returns the path or None.

    Same transport as extract_video: http(s) via the context request API (cookies
    + proxy, no CORS), blob: via in-page fetch."""
    Config.ensure_dirs()
    data: bytes
    mime = ""
    try:
        if src.startswith("blob:"):
            res = await page.evaluate(_JS_FETCH_B64, src)
            data = base64.b64decode(res.get("b64", ""))
            mime = res.get("type", "")
        else:
            resp = await page.context.request.get(src)
            if resp.ok:
                data = await resp.body()
                mime = (resp.headers or {}).get("content-type", "")
            else:
                res = await page.evaluate(_JS_FETCH_B64, src)
                data = base64.b64decode(res.get("b64", ""))
                mime = res.get("type", "")
    except Exception as e:
        log.error(f"Seedream: image download failed: {e}")
        return None
    if not data:
        return None

    ext = _IMG_EXT.get((mime or "").split(";")[0].strip().lower(), "")
    if not ext:
        ext = "webp" if "webp" in src.lower() else "png"
    safe = re.sub(r"[^\w-]", "_", alt)[:40].strip("_") or "seedream"
    path = Config.IMAGES_DIR / f"{safe}_{uuid.uuid4().hex[:10]}.{ext}"
    try:
        path.write_bytes(data)
    except Exception as e:
        log.error(f"Seedream: could not write image file: {e}")
        return None
    log.info(f"Seedream: saved image ({len(data)} bytes) -> {path.name}")
    return str(path)
