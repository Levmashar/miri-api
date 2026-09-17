"""
Grok web client — drives grok.com.

Same interface as the ChatGPT/Gemini clients so it drops straight into the
Worker/pool machinery. Selectors were read off the live grok.com DOM; the
assistant-turn rule is negative (a message bubble not inside a user-message)
because grok.com gates replies behind a sign-up wall for anonymous visitors.

Image generation ("Imagine") happens inline in the chat, so the same
generate-then-scrape flow used for the other providers applies.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.accounts.limit_detector import detect_limit, LimitInfo
from src.core.browser.human import random_delay
from src.core.config import Config
from src.providers.temporary_chat import temporary_new_chat, verify_temporary_chat
from src.core.log import setup_logging
from src.providers.chatgpt.models import ChatResponse, ImageInfo
from src.providers.grok.detector import (
    count_assistant_turns,
    extract_latest_response,
    has_generated_image,
    wait_for_response_complete,
)
from src.providers.grok.selectors import GrokSelectors as S
from src.providers.grok import models as M
from src.providers.model_picker import apply_modes, pick_from_dropdown
from src.providers.composer import enter_prompt, submit_prompt

log = setup_logging("grok_client")

GROK_URL = "https://grok.com"
_THREAD_RE = re.compile(r"/(?:chat|c)/([A-Za-z0-9_-]+)")


class GrokClient:
    """One Grok tab."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.last_image_model = ""
        # Which chat model/mode the LAST send actually ran on ("" = whatever the
        # account already had selected).
        self.last_chat_model = ""
        # Composer modes this tab currently has switched on (they survive between
        # requests, so the next request has to be able to switch them off).
        self._modes_on: list = []
        # False until this client has reconciled the UI's switches once.
        self._modes_synced = False

    @property
    def page(self) -> Page:
        return self._page

    async def _find(self, selectors, name: str, timeout: int = 8000):
        for sel in selectors:
            try:
                el = await self._page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
        log.debug(f"Grok: no selector matched for {name}")
        return None

    def _extract_thread_id(self) -> str:
        m = _THREAD_RE.search(self._page.url or "")
        return m.group(1) if m else ""

    # ── Sending ─────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list = None,
        file_paths: list = None,
        expect_image: bool = False,
        image_model: str = "",
        chat_model: str = "",
    ) -> ChatResponse:
        start = time.time()
        attachments = (image_paths or []) + (file_paths or [])

        # Model choice BEFORE typing — switching re-renders the composer and can
        # drop a half-typed prompt. Best-effort: an unavailable model/mode leaves
        # the account on its current one instead of failing the request.
        wanted_model = await self._select_chat_model(chat_model)
        await verify_temporary_chat(self._page)

        # Count the turns AFTER the model choice: picking a model can reset or
        # re-render the conversation, and a pre_count taken before that would
        # never be exceeded — the completion wait would sit there until timeout.
        pre_count = await count_assistant_turns(self._page)

        if attachments:
            await self._upload(attachments)

        await enter_prompt(self._page, text, S.CHAT_INPUT, name="Grok")
        await submit_prompt(
            self._page, text, S.CHAT_INPUT, S.SEND_BUTTON, name="Grok",
            user_selectors=[S.USER_TURN], stop_selectors=S.STOP_BUTTON,
        )

        log.info(
            f"Grok: sent ({len(text)} chars, {len(attachments)} attachment(s)"
            f"{f', model={wanted_model}' if wanted_model else ''})"
        )

        # Agentic modes (DeepSearch, Heavy) run for minutes — the normal chat
        # deadline would expire mid-run, so they get the long-mode one.
        timeout_ms = (
            Config.LONG_MODE_TIMEOUT if M.is_long(wanted_model) else Config.RESPONSE_TIMEOUT
        )
        ok = await wait_for_response_complete(
            self._page,
            pre_count=pre_count,
            timeout_ms=timeout_ms,
            expect_image=expect_image,
        )
        if not ok:
            raise RuntimeError("Grok: no complete new response before timeout")

        response_text = await extract_latest_response(self._page)

        images: list = []
        if expect_image or await has_generated_image(self._page):
            images = await self._extract_images()
        self.last_image_model = "grok-imagine" if images else ""

        elapsed_ms = int((time.time() - start) * 1000)

        limit = LimitInfo(hit=False)
        if not images:
            limit = detect_limit(response_text)
            if limit.hit:
                log.warning(f"Grok: usage-limit banner detected: {response_text[:120]}")

        log.info(
            f"Grok response ({elapsed_ms}ms, {len(response_text)} chars"
            f"{f', {len(images)} image(s)' if images else ''})"
        )

        return ChatResponse(
            message=response_text,
            thread_id=self._extract_thread_id(),
            response_time_ms=elapsed_ms,
            images=images,
            has_images=bool(images),
            limit_hit=limit.hit,
            limit_kind=limit.kind,
            limit_reset_seconds=limit.reset_seconds,
        )

    def _suspect_modes(self):
        """(groups that may be switched on, whether we know they are).

        A tab's switches survive between requests AND between sessions — the
        browser profile is persistent, so a mode switched on in an earlier
        session is still on while a freshly created client remembers nothing.
        The FIRST selection on a tab therefore treats every switch the registry
        knows as suspect, with assume_on=False: only a switch the page
        positively reports as on gets cleared, never a blind click.
        """
        if self._modes_synced:
            return self._modes_on, True
        self._modes_synced = True
        return M.all_tool_groups(), False

    async def _select_chat_model(self, chat_model: str) -> str:
        """Point the UI at the requested chat model / mode before sending.

        `chat_model` is an API id from src/providers/grok/models.py. Returns the
        id actually applied, "" when nothing was changed — an unknown id, the
        default id, or a tier this account is not offered all mean "answer on
        whatever the UI already has" rather than fail.
        """
        wanted = M.resolve_chat_model_id(chat_model)
        self.last_chat_model = ""
        active, assume_on = self._suspect_modes()

        if not wanted or wanted == M.DEFAULT_MODEL_ID:
            # Still clear any mode a previous request left on this tab.
            if active:
                _, self._modes_on = await apply_modes(
                    self._page,
                    wanted_groups=[],
                    active_groups=active,
                    assume_on=assume_on,
                    button_selectors=S.MODE_BUTTON,
                    menu_selectors=S.MODE_MENU_BUTTON,
                    menu_item_selectors=S.MODE_BUTTON,
                    log=log,
                    what="Grok mode",
                )
            return ""

        applied = False
        labels = M.label_candidates(wanted)
        if labels:
            applied = bool(await pick_from_dropdown(
                self._page,
                open_selectors=S.MODEL_MENU_BUTTON,
                item_selectors=S.MODEL_MENU_ITEM,
                labels=labels,
                log=log,
                what=f"Grok model {wanted}",
            ))

        # Modes are sticky per tab, so this also switches OFF a mode a previous
        # request on this tab turned on (self._modes_on remembers those).
        enabled, self._modes_on = await apply_modes(
            self._page,
            wanted_groups=M.tool_groups(wanted),
            active_groups=active,
            assume_on=assume_on,
            button_selectors=S.MODE_BUTTON,
            menu_selectors=S.MODE_MENU_BUTTON,
            menu_item_selectors=S.MODE_BUTTON,
            log=log,
            what=f"Grok mode {wanted}",
        )
        applied = applied or enabled

        self.last_chat_model = wanted if applied else ""
        return self.last_chat_model

    async def _upload(self, paths: list) -> None:
        try:
            inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                b = await self._find(S.UPLOAD_BUTTON, "upload button", timeout=4000)
                if b is not None:
                    await b.click()
                    await random_delay(400, 800)
                inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                log.warning("Grok: no file input found — sending without attachments")
                return
            await inp.set_input_files(paths)
            await asyncio.sleep(2 + 1.0 * len(paths))
            log.info(f"Grok: attached {len(paths)} file(s)")
        except Exception as e:
            log.error(f"Grok: attachment upload failed: {e}")

    async def _extract_images(self) -> list:
        """Extract generated images from the latest assistant turn.

        Grok ("Imagine") serves generated images from an AUTHENTICATED CDN, so a
        bare urllib fetch gets 403 Forbidden and a cross-origin <canvas> read can
        taint. We pull the bytes with the browser CONTEXT request API — it reuses
        the session cookies and the account's proxy and isn't subject to page
        CORS — and fall back to an in-page canvas read for blob/same-origin
        images. This is the same approach the Gemini/Seedream extractors use.
        """
        import base64
        import re as _re
        import uuid as _uuid

        try:
            items = await self._page.evaluate(
                """
                () => {
                    const all = Array.from(document.querySelectorAll('.message-bubble'))
                        .filter(el => !el.closest('[data-testid="user-message"]'));
                    if (!all.length) return [];
                    const last = all[all.length - 1];
                    const out = [];
                    last.querySelectorAll('img').forEach(img => {
                        const w = img.naturalWidth || img.width || 0;
                        const h = img.naturalHeight || img.height || 0;
                        const src = img.src || '';
                        if (src && w >= 200 && h >= 200) out.push({ src, alt: img.alt || '' });
                    });
                    return out;
                }
                """
            ) or []
        except Exception as e:
            log.error(f"Grok: image scan failed: {e}")
            items = []

        Config.ensure_dirs()
        _ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
        images, seen = [], set()
        for item in items:
            src = item.get("src", "")
            if not src or src in seen:
                continue
            seen.add(src)
            alt = item.get("alt", "") or "Grok image"
            data, ext = None, "png"

            # 1. Authenticated fetch via the browser context (cookies + proxy).
            if src.startswith("http"):
                try:
                    resp = await self._page.context.request.get(src)
                    if resp.ok:
                        data = await resp.body()
                        ext = _ext.get((resp.headers or {}).get("content-type", "").split(";")[0].strip(), "png")
                    else:
                        log.debug(f"Grok: context fetch {resp.status} for {src[:80]}")
                except Exception as e:
                    log.debug(f"Grok: context fetch failed: {e}")

            # 2. Fallback: in-page canvas -> PNG (blob / same-origin images).
            if not data:
                try:
                    dataurl = await self._page.evaluate(
                        """
                        (src) => {
                            const img = Array.from(document.images).find(i => i.src === src);
                            if (!img) return '';
                            try {
                                const c = document.createElement('canvas');
                                c.width = img.naturalWidth; c.height = img.naturalHeight;
                                c.getContext('2d').drawImage(img, 0, 0);
                                return c.toDataURL('image/png');
                            } catch (e) { return ''; }
                        }
                        """,
                        src,
                    )
                    if dataurl.startswith("data:image") and "," in dataurl:
                        data, ext = base64.b64decode(dataurl.split(",", 1)[1]), "png"
                except Exception as e:
                    log.debug(f"Grok: canvas fallback failed: {e}")

            if not data:
                log.warning(f"Grok: could not download generated image {src[:80]}")
                continue

            safe = _re.sub(r"[^\w-]", "_", alt)[:40].strip("_") or "grok"
            path = Config.IMAGES_DIR / f"{safe}_{_uuid.uuid4().hex[:10]}.{ext}"
            try:
                path.write_bytes(data)
                images.append(ImageInfo(url=src, alt=alt, local_path=str(path), prompt_title=alt))
            except Exception as e:
                log.error(f"Grok: could not save image: {e}")
        if images:
            log.info(f"Grok: extracted {len(images)} image(s)")
        return images

    # ── Navigation ──────────────────────────────────────────────

    @temporary_new_chat
    async def new_chat(self) -> None:
        try:
            await self._page.goto(GROK_URL, wait_until="domcontentloaded")
            await random_delay(700, 1300)
            log.info("Grok: new chat")
        except Exception as e:
            log.error(f"Grok: new_chat failed: {e}")
            raise

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Open an existing conversation and wait for its turns to render.

        The wait matters: send_message counts existing turns straight after
        this, and counting 0 on a half-loaded page would make the detector think
        the reply already arrived and extract a STALE answer.
        """
        url = f"{GROK_URL}/chat/{thread_id}"
        log.info(f"Grok: navigating to thread {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        try:
            await self._page.wait_for_selector(S.MESSAGE_BUBBLE, timeout=15000)
        except Exception:
            log.warning(f"Grok: thread {thread_id} rendered no turns within 15s")
        prev = -1
        for _ in range(20):
            await asyncio.sleep(0.2)
            count = await count_assistant_turns(self._page)
            if count == prev and count > 0:
                break
            prev = count
        log.info(f"Grok: thread {thread_id} loaded ({prev} turn(s))")

    async def get_current_thread_url(self) -> str:
        return self._page.url

    async def list_threads(self) -> list:
        return []
