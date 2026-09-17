"""
Image handler — detects, extracts, and downloads generated images.

When ChatGPT generates an image via DALL-E, the response contains:
- An <img> tag with the image URL (hosted on openai.com)
- A "Image created" text indicator
- An image title/alt text (description of what was generated)

This module:
1. Detects if the last assistant message contains generated images
2. Extracts image URLs and metadata
3. Downloads images to local disk
4. Returns ImageInfo objects with URLs and local paths
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from patchright.async_api import Page

from src.core.config import Config
from src.providers.chatgpt.selectors import Selectors
from src.providers.chatgpt.models import ImageInfo
from src.core.log import setup_logging

log = setup_logging("image_handler")

# Cap for the non-browser fallback fetch, so a hostile/huge URL cannot fill the disk.
_MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB


async def detect_images_in_response(page: Page) -> list[dict]:
    """
    Check the last conversation turn for generated images.

    ChatGPT DALL-E image responses do NOT use data-message-author-role.
    Instead, images appear inside an article turn with:
    - img[alt="Generated image"]
    - div[id^="image-"] containers
    - src from chatgpt.com/backend-api/estuary/content

    Returns a list of dicts: [{url, alt, title}, ...] or empty list.
    """
    result = await page.evaluate("""
        () => {
            const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
            if (turns.length === 0) return { images: [], debug: [] };

            // Scan the last ASSISTANT turn, not simply the last turn.
            // A user turn carries the image THEY uploaded (a blob: thumbnail
            // with an alt like "uploaded image"), which matches the generated-
            // image heuristics below. Without this check, a vision request
            // whose reply never arrived would hand the caller their own upload
            // back as a "generated" image.
            let lastTurn = null;
            for (let i = turns.length - 1; i >= 0; i--) {
                const turn = turns[i];
                const isAssistant = turn.getAttribute('data-turn') === 'assistant' ||
                    Boolean(turn.querySelector('[data-message-author-role="assistant"]'));
                if (isAssistant) { lastTurn = turn; break; }
            }
            if (!lastTurn) return { images: [], debug: [] };

            // Helper: is this a generated (DALL-E) image and not an avatar/icon/UI glyph?
            const isGenerated = (img) => {
                const src = img.src || '';
                const alt = (img.alt || '').toLowerCase();
                const w = img.naturalWidth || img.width || 0;
                const h = img.naturalHeight || img.height || 0;

                // Exclude obvious non-content images
                if (!src) return false;
                if (src.startsWith('data:image/svg')) return false;      // inline icons
                if (alt.includes('avatar') || alt.includes('user')) return false;
                if (src.includes('/avatar') || src.includes('profile')) return false;

                // Positive signals (domain-agnostic — ChatGPT serves DALL-E
                // images from oaiusercontent.com, chatgpt.com, or blob: URLs)
                if (alt.includes('generated') || alt.includes('image')) return true;
                if (src.includes('oaiusercontent.com')) return true;
                if (src.includes('backend-api/estuary')) return true;
                if (src.includes('backend-api/content')) return true;
                if (src.startsWith('blob:')) return true;

                // Fallback: any reasonably large raster image in the turn
                if ((w >= 200 && h >= 200) || (w === 0 && h === 0 && src.startsWith('http'))) {
                    // Inside a known image container?
                    if (img.closest('div[id^="image-"], [class*="imagegen"]')) return true;
                    // Large enough to be content, not a UI glyph
                    if (w >= 256 && h >= 256) return true;
                }
                return false;
            };

            // Debug: record every <img> in the last turn so we can diagnose
            const allImgs = [...lastTurn.querySelectorAll('img')];
            const debug = allImgs.map((img) => ({
                src: (img.src || '').slice(0, 120),
                alt: (img.alt || '').slice(0, 60),
                w: img.naturalWidth || img.width || 0,
                h: img.naturalHeight || img.height || 0,
                matched: isGenerated(img),
            }));

            // Primary: alt="Generated image"; broaden via isGenerated()
            let images = [...lastTurn.querySelectorAll('img[alt="Generated image"]')];
            if (images.length === 0) {
                images = allImgs.filter(isGenerated);
            }

            if (!images || images.length === 0) return { images: [], debug };

            // Deduplicate by src URL
            const seen = new Set();
            const results = [];

            for (const img of images) {
                const src = img.src || '';
                if (!src || seen.has(src)) continue;
                seen.add(src);

                const alt = img.alt || '';

                // Extract the image title from nearby text in the turn
                // ChatGPT shows "Creating image • Image Title" in a button/span
                let title = '';
                const buttons = lastTurn.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = (btn.innerText || '').trim();
                    // Parse "Creating image • Title" or just "Title"
                    const bulletIdx = text.indexOf('•');
                    if (bulletIdx > -1) {
                        title = text.substring(bulletIdx + 1).trim();
                        break;
                    }
                }
                // Fallback: look for text spans in the turn
                if (!title) {
                    const spans = lastTurn.querySelectorAll(
                        'span.text-token-text-tertiary'
                    );
                    for (const span of spans) {
                        const t = (span.innerText || '').trim();
                        if (t.length > 5 && t.length < 200) {
                            title = t;
                            break;
                        }
                    }
                }

                results.push({ url: src, alt, title });
            }

            return { images: results, debug };
        }
    """)

    images = (result or {}).get("images", [])
    debug = (result or {}).get("debug", [])

    if images:
        log.info(f"Detected {len(images)} generated image(s) in response")
        for i, img in enumerate(images):
            log.debug(f"  Image {i+1}: alt='{img.get('alt', '')[:50]}', url={img.get('url', '')[:80]}...")
    else:
        log.debug("No generated images detected in response")

    # Log what <img> elements were present — ChatGPT changes its DOM
    # periodically, and this is what identifies a detection miss.
    # Logged at WARNING when nothing matched, since that is a real failure.
    if debug:
        emit = log.debug if images else log.warning
        emit(f"[img-debug] {len(debug)} <img> in last turn (matched {len(images)}):")
        for d in debug:
            emit(
                f"[img-debug]   matched={d.get('matched')} "
                f"w={d.get('w')} h={d.get('h')} alt='{d.get('alt', '')}' "
                f"src={d.get('src', '')}"
            )

    return images


async def download_image(page: Page, url: str, filename_hint: str = "") -> str:
    """
    Download an image from a URL using the browser's fetch API.

    Uses the browser context so cookies/auth are preserved (required
    for OpenAI-hosted images that may need authentication).

    Returns the local file path.
    """
    Config.ensure_dirs()

    # Generate a filename from the URL or hint
    if filename_hint:
        # Clean the hint for use as filename
        safe_name = re.sub(r'[^\w\s-]', '', filename_hint)[:60].strip()
        safe_name = re.sub(r'\s+', '_', safe_name)
    else:
        # Use hash of URL as filename
        safe_name = hashlib.md5(url.encode()).hexdigest()[:12]

    # Timestamp + random suffix to avoid collisions. Whole-second ts alone is
    # NOT unique under concurrency: two workers producing images with the same
    # title in the same second would collide and one would overwrite the other
    # before it was read. The uuid makes each filename unique per download.
    ts = int(time.time())
    unique = uuid.uuid4().hex[:8]
    filename = f"{safe_name}_{ts}_{unique}.png"
    local_path = Config.IMAGES_DIR / filename

    log.info(f"Downloading image to {local_path}...")

    try:
        # Use browser's fetch to download (preserves auth cookies)
        image_data = await page.evaluate("""
            async (url) => {
                try {
                    const response = await fetch(url);
                    if (!response.ok) return null;
                    const blob = await response.blob();
                    const reader = new FileReader();
                    return new Promise((resolve) => {
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch (e) {
                    return null;
                }
            }
        """, url)

        if image_data and image_data.startswith("data:"):
            # Strip the data URL prefix to get raw base64
            import base64
            header, b64data = image_data.split(",", 1)

            # Detect actual format from MIME type
            if "png" in header:
                ext = ".png"
            elif "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
            else:
                ext = ".png"

            # Update filename with correct extension (keep the unique suffix)
            filename = f"{safe_name}_{ts}_{unique}{ext}"
            local_path = Config.IMAGES_DIR / filename

            raw_bytes = base64.b64decode(b64data)
            local_path.write_bytes(raw_bytes)

            size_kb = len(raw_bytes) / 1024
            log.info(f"Image saved: {local_path} ({size_kb:.1f} KB)")
            return str(local_path)

        else:
            log.warning("Failed to fetch image data via browser")

    except Exception as e:
        log.error(f"Image download failed: {e}", exc_info=True)

    # Fallback: fetch directly, outside the browser.
    #
    # Only http/https: `url` comes from an <img src> in the page DOM, so it is
    # provider-controlled. urlretrieve honours any scheme it is given, so a
    # src of file:///etc/passwd would read a local file and hand it back to the
    # API caller as a "generated image".
    #
    # Note this path rarely succeeds for real generated images: they sit behind
    # authenticated URLs and this request carries no session cookies. The
    # browser fetch above is the path that works.
    if not url.lower().startswith(("http://", "https://")):
        log.error(f"Refusing to fetch image over non-http scheme: {url[:60]}")
        return ""

    try:
        import urllib.request

        def _fetch() -> None:
            req = urllib.request.Request(url, headers={"User-Agent": "miri-api"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError(f"image exceeds {_MAX_IMAGE_BYTES} bytes")
            local_path.write_bytes(data)

        # Blocking I/O — must not run inline on the event loop, which would
        # freeze every other request (and /healthz) for the download's duration.
        await asyncio.to_thread(_fetch)
        log.info(f"Image saved via urllib: {local_path}")
        return str(local_path)
    except Exception as e2:
        log.error(f"Fallback download also failed: {e2}")

    return ""


async def extract_images_from_response(
    page: Page,
    expect_image: bool = False,
) -> list[ImageInfo]:
    """
    Full pipeline: detect images in the last response, download them,
    and return ImageInfo objects with both URLs and local paths.

    expect_image:
        True  — the request explicitly asked for an image, so poll for a
                few seconds: DALL-E can still be painting the <img> into
                the DOM after the turn otherwise looks complete.
        False — plain text request. Check once and move on; retrying here
                would add seconds of latency to every text response.
    """
    raw_images = await detect_images_in_response(page)

    if expect_image:
        for attempt in range(1, 5):
            if raw_images:
                break
            log.info(f"No image yet — retrying detection ({attempt}/4) after short wait")
            await asyncio.sleep(1.5)
            raw_images = await detect_images_in_response(page)

    if not raw_images:
        return []

    image_infos = []
    for img_data in raw_images:
        url = img_data.get("url", "")
        alt = img_data.get("alt", "")
        title = img_data.get("title", "")

        # Download the image
        hint = alt or title or "chatgpt_image"
        local_path = await download_image(page, url, filename_hint=hint)

        image_infos.append(ImageInfo(
            url=url,
            alt=alt,
            local_path=local_path,
            prompt_title=title,
        ))

    log.info(f"Processed {len(image_infos)} image(s)")
    return image_infos
