"""
Gemini web client — drives gemini.google.com/app.

Implements the SAME interface as ChatGPTClient so it drops straight into the
existing Worker/pool machinery:
    send_message(text, image_paths=, file_paths=, expect_image=) -> ChatResponse
    new_chat(), navigate_to_thread(id), _extract_thread_id(), list_threads()

Selectors were read off the live DOM (see selectors.py). Verified end-to-end:
sending "Reply with exactly the word: PING" returns "PING", and the URL becomes
https://gemini.google.com/app/<hex id> — that id is the thread id, the direct
analogue of ChatGPT's /c/<uuid>.
"""

from __future__ import annotations

import asyncio
import re
import time

from patchright.async_api import Page

from src.accounts.limit_detector import detect_limit, LimitInfo
from src.providers.chatgpt.models import ChatResponse, ImageInfo
from src.core.browser.human import random_delay
from src.core.config import Config
from src.providers.temporary_chat import temporary_new_chat, verify_temporary_chat
from src.core.log import setup_logging
from src.providers.gemini.detector import (
    count_model_responses,
    extract_latest_response,
    has_generated_image,
    wait_for_response_complete,
)
from src.providers.gemini.selectors import GeminiSelectors as S
from src.providers.gemini import models as M
from src.providers.model_picker import apply_modes, click_confirm, pick_from_dropdown
from src.providers.composer import enter_prompt, submit_prompt

log = setup_logging("gemini_client")

_THREAD_RE = re.compile(r"/app/([A-Za-z0-9_-]+)")

GEMINI_URL = "https://gemini.google.com/app"


class GeminiClient:
    """One Gemini tab."""

    def __init__(self, page: Page) -> None:
        self._page = page
        # Which image / chat model the LAST send actually ran on ("" = whatever
        # the account already had selected). Reported in logs, not guessed at.
        self.last_image_model = ""
        self.last_chat_model = ""
        # Composer modes this tab currently has switched on (they survive between
        # requests, so the next request has to be able to switch them off).
        self._modes_on: list = []
        # False until this client has reconciled the UI's switches once.
        self._modes_synced = False

    @property
    def page(self) -> Page:
        return self._page

    # ── Helpers ─────────────────────────────────────────────────

    async def _find(self, selectors, name: str, timeout: int = 8000):
        """First selector that resolves, or None."""
        for sel in selectors:
            try:
                el = await self._page.wait_for_selector(sel, timeout=timeout, state="visible")
                if el:
                    return el
            except Exception:
                continue
        log.debug(f"Gemini: no selector matched for {name}")
        return None

    def _extract_thread_id(self) -> str:
        """Thread id from the URL (/app/<id>), '' on the fresh /app page."""
        m = _THREAD_RE.search(self._page.url or "")
        return m.group(1) if m else ""

    # ── Sending ─────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list[str] | None = None,
        file_paths: list[str] | None = None,
        expect_image: bool = False,
        image_model: str = "",
        chat_model: str = "",
    ) -> ChatResponse:
        start = time.time()
        attachments = (image_paths or []) + (file_paths or [])

        # 0. Model choice. Done BEFORE typing: switching model re-renders the
        #    composer, and a half-typed prompt can be lost with it. Best-effort —
        #    an unavailable model leaves the account on its current one.
        wanted_model = await self._select_chat_model(chat_model)
        await verify_temporary_chat(self._page)

        # Count the turns AFTER the model choice: picking a model can reset or
        # re-render the conversation, and a pre_count taken before that would
        # never be exceeded — the completion wait would sit there until timeout.
        pre_count = await count_model_responses(self._page)

        # 1. Attachments first — the composer must already hold them when we send.
        if attachments:
            await self._upload(attachments)

        # Insert the entire draft without Enter events, then wait for the UI to
        # acknowledge submission before starting the generation deadline.
        await enter_prompt(self._page, text, S.CHAT_INPUT, name="Gemini")
        await submit_prompt(
            self._page, text, S.CHAT_INPUT, S.SEND_BUTTON, name="Gemini",
            user_selectors=[S.USER_TURN], stop_selectors=S.STOP_BUTTON,
        )

        log.info(
            f"Gemini: sent ({len(text)} chars, {len(attachments)} attachment(s)"
            f"{f', model={wanted_model}' if wanted_model else ''})"
        )

        # 3b. Deep Research answers with a PLAN and waits for a confirmation
        #     click before it does the actual research. Do that here, while the
        #     completion wait has not started, so the plan can't be mistaken for
        #     the final report.
        if wanted_model == "gemini-deep-research":
            await click_confirm(
                self._page,
                labels=M.START_RESEARCH_LABELS,
                selectors=S.START_RESEARCH_BUTTON,
                log=log,
            )

        # 4. Wait for the answer to settle. Slow modes (Deep Research, Deep
        #    Think) get the long deadline — the normal one expires mid-run.
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
            raise RuntimeError("Gemini: no complete new response before timeout")

        response_text = await extract_latest_response(self._page)

        # 5. Images. The app generates with Nano Banana 2; if this was an image
        #    request and NB Pro is enabled, try to upgrade the result first.
        # The model id decides the variant: *-pro runs the "Redo with Nano
        # Banana Pro" upgrade (paid plans only); otherwise keep the Nano Banana 2
        # result. If the caller didn't specify, fall back to the config default.
        want_pro = (image_model or "").lower().endswith("pro") or (
            not image_model and Config.GEMINI_USE_NANO_BANANA_PRO
        )
        images: list[ImageInfo] = []
        used_nb_pro = False
        if expect_image or await has_generated_image(self._page):
            if expect_image and want_pro:
                used_nb_pro = await self._upgrade_to_nano_banana_pro()
            images = await self._extract_images()
        self.last_image_model = (
            "nano-banana-pro" if used_nb_pro else "nano-banana"
        )

        elapsed_ms = int((time.time() - start) * 1000)

        # Usage-limit banner detection, same contract as the ChatGPT client so
        # the account layer can cool this account down.
        limit = LimitInfo(hit=False)
        if not images:
            limit = detect_limit(response_text)
            if limit.hit:
                log.warning(f"Gemini: usage-limit banner detected: {response_text[:120]}")

        log.info(
            f"Gemini response ({elapsed_ms}ms, {len(response_text)} chars"
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

        `chat_model` is an API id from src/providers/gemini/models.py. Returns
        the id that was actually applied, "" when nothing was changed — an
        unknown id, the default id, or a model this account is not offered all
        mean "answer on whatever the UI already has" rather than fail.
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
                    button_selectors=S.TOOL_BUTTON,
                    menu_selectors=S.TOOLS_MENU_BUTTON,
                    menu_item_selectors=S.TOOL_BUTTON,
                    log=log,
                    what="Gemini mode",
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
                what=f"Gemini model {wanted}",
            ))

        # Modes are sticky per tab, so this also switches OFF a mode a previous
        # request on this tab turned on (self._modes_on remembers those).
        enabled, self._modes_on = await apply_modes(
            self._page,
            wanted_groups=M.tool_groups(wanted),
            active_groups=active,
            assume_on=assume_on,
            button_selectors=S.TOOL_BUTTON,
            menu_selectors=S.TOOLS_MENU_BUTTON,
            menu_item_selectors=S.TOOL_BUTTON,
            log=log,
            what=f"Gemini mode {wanted}",
        )
        applied = applied or enabled

        self.last_chat_model = wanted if applied else ""
        return self.last_chat_model

    async def _upload(self, paths: list[str]) -> None:
        """Attach files via the hidden <input type=file>."""
        try:
            inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                btn = await self._find(S.UPLOAD_BUTTON, "upload button", timeout=4000)
                if btn is not None:
                    await btn.click()
                    await random_delay(400, 800)
                inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                log.warning("Gemini: no file input found — sending without attachments")
                return
            await inp.set_input_files(paths)
            # Give the upload a moment to register in the composer.
            await asyncio.sleep(2 + 1.0 * len(paths))
            log.info(f"Gemini: attached {len(paths)} file(s)")
        except Exception as e:
            log.error(f"Gemini: attachment upload failed: {e}")

    async def _upgrade_to_nano_banana_pro(self) -> bool:
        """Regenerate the just-produced image with Nano Banana Pro.

        Since Feb 2026 the Gemini app generates with Nano Banana 2 and exposes
        Nano Banana Pro only as a "Redo with Nano Banana Pro" action in the
        result's overflow menu — and only on PAID Google AI plans. So this is a
        best-effort second step: on a free account the menu entry simply is not
        there, and we keep the Nano Banana 2 image rather than failing.

        Returns True only if the Pro regeneration was actually triggered.
        """
        try:
            # 1. Open the overflow menu on the latest result.
            btn = None
            for sel in S.IMAGE_OVERFLOW_BUTTON:
                els = await self._page.query_selector_all(sel)
                if els:
                    btn = els[-1]  # the newest response
                    break
            if btn is None:
                log.info("Gemini: no image overflow menu — keeping Nano Banana 2 result")
                return False
            await btn.click()
            await random_delay(500, 900)

            # 2. Find the menu entry BY TEXT (markup churns, labels don't).
            clicked = await self._page.evaluate(
                """
                (needle) => {
                    const nodes = Array.from(document.querySelectorAll(
                        '[role="menuitem"], button, [mat-menu-item], .mat-mdc-menu-item'
                    ));
                    for (const n of nodes) {
                        const t = (n.innerText || n.textContent || '').toLowerCase();
                        if (t.includes(needle)) { n.click(); return true; }
                    }
                    return false;
                }
                """,
                S.NANO_BANANA_PRO_MENU_TEXT,
            )
            if not clicked:
                log.info(
                    "Gemini: 'Redo with Nano Banana Pro' not offered "
                    "(needs a paid Google AI plan) — keeping Nano Banana 2 result"
                )
                await self._page.keyboard.press("Escape")
                return False

            log.info("Gemini: regenerating with Nano Banana Pro...")
            pre = await count_model_responses(self._page)
            # The redo replaces/appends a response; wait for it to settle.
            await wait_for_response_complete(
                self._page,
                pre_count=max(0, pre - 1),
                timeout_ms=Config.RESPONSE_TIMEOUT,
                expect_image=True,
            )
            log.info("Gemini: Nano Banana Pro regeneration complete")
            return True
        except Exception as e:
            log.warning(f"Gemini: Nano Banana Pro upgrade failed ({e}) — keeping original")
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _extract_images(self) -> list[ImageInfo]:
        """Extract images from the latest assistant turn.

        Gemini renders generated images as blob: URLs, which urllib/fetch can't
        reliably download (blobs get revoked, are page-scoped). So we read each
        <img> straight off the canvas in the browser to a PNG data URL — that
        works for blob AND http images and never depends on the URL surviving.
        """
        import base64
        import uuid as _uuid

        items = []
        try:
            items = await self._page.evaluate(
                """
                () => {
                    const turns = document.querySelectorAll('model-response');
                    if (!turns.length) return [];
                    const last = turns[turns.length - 1];
                    const out = [];
                    last.querySelectorAll('img').forEach(img => {
                        const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
                        if (w < 240 || h < 240) return;
                        let dataurl = '';
                        try {
                            const c = document.createElement('canvas');
                            c.width = w; c.height = h;
                            c.getContext('2d').drawImage(img, 0, 0);
                            dataurl = c.toDataURL('image/png');
                        } catch (e) { dataurl = ''; }
                        out.push({ dataurl, alt: img.alt || '', src: (img.src||'').slice(0,60), w, h });
                    });
                    return out;
                }
                """
            ) or []
        except Exception as e:
            log.error(f"Gemini: image scan failed: {e}")

        if not items:
            log.warning("Gemini: no qualifying image in the latest turn")
            return []

        Config.ensure_dirs()
        images: list[ImageInfo] = []
        seen = set()
        for item in items:
            data = item.get("dataurl", "")
            alt = item.get("alt", "") or "Gemini image"
            if not data.startswith("data:image") or "," not in data:
                log.warning(f"Gemini: image had no readable canvas data (src={item.get('src')}) — tainted?")
                continue
            b64 = data.split(",", 1)[1]
            if b64 in seen:
                continue
            seen.add(b64)
            try:
                raw = base64.b64decode(b64)
                safe = re.sub(r"[^\w-]", "_", alt)[:40].strip("_") or "gemini"
                fn = f"{safe}_{_uuid.uuid4().hex[:10]}.png"
                path = Config.IMAGES_DIR / fn
                path.write_bytes(raw)
                images.append(ImageInfo(url=item.get("src", ""), alt=alt,
                                        local_path=str(path), prompt_title=alt))
            except Exception as e:
                log.error(f"Gemini: could not save image: {e}")
        if images:
            log.info(f"Gemini: extracted {len(images)} image(s) via canvas")
        return images

    # ── Navigation ──────────────────────────────────────────────

    @temporary_new_chat
    async def new_chat(self) -> None:
        """Start a fresh conversation."""
        try:
            await self._page.goto(GEMINI_URL, wait_until="domcontentloaded")
            await random_delay(700, 1300)
            log.info("Gemini: new chat")
        except Exception as e:
            log.error(f"Gemini: new_chat failed: {e}")
            raise

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Open an existing conversation and wait for its turns to render.

        The wait matters: send_message() counts existing turns immediately after
        this returns, and counting 0 on a half-loaded page makes the detector
        think the reply already arrived and extract a STALE answer.
        """
        url = f"{GEMINI_URL}/{thread_id}"
        log.info(f"Gemini: navigating to thread {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        try:
            await self._page.wait_for_selector(S.MODEL_TURN, timeout=15000)
        except Exception:
            log.warning(f"Gemini: thread {thread_id} rendered no turns within 15s")
        prev = -1
        for _ in range(20):
            await asyncio.sleep(0.2)
            count = await count_model_responses(self._page)
            if count == prev and count > 0:
                break
            prev = count
        log.info(f"Gemini: thread {thread_id} loaded ({prev} turn(s))")

    async def get_current_thread_url(self) -> str:
        return self._page.url

    async def list_threads(self) -> list[dict]:
        """Recent conversations from the side nav (best-effort)."""
        try:
            return await self._page.evaluate(
                """
                () => Array.from(document.querySelectorAll('[data-test-id="conversation"], .conversation-title'))
                    .slice(0, 20)
                    .map((el, i) => ({ id: el.getAttribute('jslog') || String(i),
                                       title: (el.innerText || '').trim().slice(0, 80),
                                       url: location.origin + '/app' }))
                """
            ) or []
        except Exception:
            return []
