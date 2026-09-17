"""
Seedream web client — drives Dreamina (dreamina.capcut.com) VIDEO generation.

Implements the same small interface the Worker/pool expect (send_message,
new_chat, navigate_to_thread, _extract_thread_id, list_threads) PLUS a rich
generate_video() that the /v1/videos endpoints call. Seedream is video-only, so
send_message() (chat/image) refuses — those endpoints are already 501'd upstream.

Flow (mirrors the verified 10x-chat driver):
  goto home?type=video -> pick model/aspect/resolution/duration -> set ref mode
  -> upload images -> type prompt -> snapshot baseline video srcs -> submit
  -> wait for /ai-tool/generate -> poll for a NEW finished <video> -> download.

Every selection (model/aspect/resolution/duration/ref-mode) is BEST-EFFORT: if a
control or option is absent for the account/plan, we log and continue rather than
fail, because the live UI is the authority (see selectors.py). The must-work path
is prompt -> submit -> poll -> download.
"""

from __future__ import annotations

import asyncio
import time

from patchright.async_api import Page

from src.core.browser.human import random_delay
from src.core.config import Config
from src.providers.temporary_chat import fresh_chat_required, temporary_new_chat
from src.core.log import setup_logging
from src.providers.chatgpt.models import ChatResponse, ImageInfo, VideoInfo
from src.providers.seedream import models as M
from src.providers.seedream.detector import (
    capture_image_srcs,
    capture_video_srcs,
    extract_image,
    extract_video,
    wait_for_new_images,
    wait_for_new_video,
)
from src.providers.seedream.selectors import SeedreamSelectors as S
from src.providers.composer import enter_prompt, submit_prompt

log = setup_logging("seedream_client")


class SeedreamClient:
    """One Dreamina video-generation tab."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.last_video_model = ""
        self.last_image_model = ""

    @property
    def page(self) -> Page:
        return self._page

    # ── Interface stubs (Seedream is video-only) ────────────────

    def _extract_thread_id(self) -> str:
        # Video generations are not conversational threads — no chaining.
        return ""

    async def send_message(self, text: str, image_paths=None, file_paths=None,
                           expect_image: bool = False, image_model: str = "",
                           chat_model: str = "") -> ChatResponse:
        """Image generation (Dreamina image tab).

        Seedream has no chat/text surface, so this IS the image entry point the
        /v1/images/* endpoints call (chat/responses are 501'd upstream). Reference
        images drive image-to-image / multi-image fusion."""
        return await self._generate_image(
            prompt=text, image_paths=image_paths, image_model=image_model
        )

    async def navigate_to_thread(self, thread_id: str) -> None:  # pragma: no cover
        await self.new_chat()

    async def list_threads(self) -> list:
        return []

    async def get_current_thread_url(self) -> str:
        return self._page.url

    @temporary_new_chat
    async def new_chat(self) -> None:
        """Reset to a fresh generate view."""
        try:
            await self._page.goto(S.HOME_URL, wait_until="domcontentloaded")
            await random_delay(800, 1500)
        except Exception as e:
            log.error(f"Seedream: new_chat failed: {e}")
            raise

    async def _goto_image_home(self) -> None:
        """Navigate to the Dreamina IMAGE-generation view."""
        try:
            await self._page.goto(S.IMAGE_HOME_URL, wait_until="domcontentloaded")
            await random_delay(800, 1500)
        except Exception as e:
            log.error(f"Seedream: goto image home failed: {e}")
            raise

    # ── UI helpers (all best-effort) ────────────────────────────

    async def _select_dropdown_option(self, needles: "list[str]") -> bool:
        """Open each lv-select in turn and click the first option matching one of
        `needles` (exact match preferred, then substring). Returns True on click.

        We don't assume WHICH dropdown is model vs ref-mode vs duration — we find
        the one that offers the wanted option. Options may be portaled to <body>."""
        needles = [n.strip().lower() for n in needles if n and n.strip()]
        if not needles:
            return False
        try:
            triggers = await self._page.query_selector_all(S.SELECT_TRIGGER)
        except Exception:
            triggers = []
        for trig in triggers:
            try:
                await trig.click()
            except Exception:
                continue
            await random_delay(200, 450)
            clicked = await self._page.evaluate(
                """
                (needles) => {
                    const opts = Array.from(document.querySelectorAll(
                        '.lv-select-option, .lv-select-popup [role="option"]'));
                    const norm = s => (s || '').trim().toLowerCase();
                    // exact first
                    for (const o of opts) {
                        const t = norm(o.innerText || o.textContent);
                        if (needles.some(n => t === n)) { o.click(); return true; }
                    }
                    // then substring
                    for (const o of opts) {
                        const t = norm(o.innerText || o.textContent);
                        if (t && needles.some(n => t.includes(n))) { o.click(); return true; }
                    }
                    return false;
                }
                """,
                needles,
            )
            if clicked:
                await random_delay(200, 400)
                return True
            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass
        return False

    async def _click_lv_button(self, needle: str, *, exact: bool = True) -> bool:
        """Click a button.lv-btn whose visible text matches `needle`."""
        needle = (needle or "").strip().lower()
        if not needle:
            return False
        try:
            return bool(await self._page.evaluate(
                """
                (args) => {
                    const [needle, exact] = args;
                    const btns = Array.from(document.querySelectorAll('button.lv-btn, button'));
                    const norm = s => (s || '').trim().toLowerCase();
                    for (const b of btns) {
                        const t = norm(b.innerText || b.textContent);
                        if (!t) continue;
                        if (exact ? t === needle : t.includes(needle)) {
                            if (b.offsetParent !== null) { b.click(); return true; }
                        }
                    }
                    return false;
                }
                """,
                [needle, exact],
            ))
        except Exception:
            return False

    async def _set_model(self, model_id: str) -> None:
        label = M.label_for(model_id)
        if not label:
            return
        if await self._select_dropdown_option([label]):
            log.info(f"Seedream: selected model '{label}'")
        else:
            log.info(f"Seedream: model '{label}' not offered — keeping current selection")

    async def _set_ref_mode(self, mode: str) -> None:
        value = M.MODE_TO_REF_MODE.get(mode)
        if not value:
            return
        labels = list(M.REF_MODE_LABELS.get(value, ()))
        if await self._select_dropdown_option(labels):
            log.info(f"Seedream: reference mode -> {value}")
        else:
            log.info(f"Seedream: reference-mode option for '{value}' not found — best-effort continue")

    async def _set_aspect(self, aspect: str) -> None:
        if aspect and await self._click_lv_button(aspect, exact=True):
            log.info(f"Seedream: aspect {aspect}")

    async def _set_resolution(self, resolution: str) -> None:
        if not resolution:
            return
        # UI shows "720P"/"1080P"; accept "720p"/"1080" too.
        cand = resolution.strip().lower().replace("p", "")
        for text in (resolution, f"{cand}p", f"{cand}P".upper()):
            if await self._click_lv_button(text, exact=False):
                log.info(f"Seedream: resolution {resolution}")
                return

    async def _set_duration(self, duration_s: float) -> None:
        if not duration_s:
            return
        n = int(round(duration_s))
        # Duration is one of the lv-select dropdowns; try "5s" / "5 s".
        if await self._select_dropdown_option([f"{n}s", f"{n} s", f"{n} seconds"]):
            log.info(f"Seedream: duration {n}s")

    async def _upload(self, paths: "list[str]") -> None:
        if not paths:
            return
        try:
            inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                log.warning("Seedream: no file input found — sending without images")
                return
            await inp.set_input_files(paths)
            await asyncio.sleep(2 + 1.2 * len(paths))  # let uploads register
            log.info(f"Seedream: uploaded {len(paths)} image(s)")
        except Exception as e:
            log.error(f"Seedream: image upload failed: {e}")

    async def _type_prompt(self, text: str) -> None:
        """Insert and verify the complete ProseMirror composer draft."""
        await enter_prompt(self._page, text, S.COMPOSER, name="Seedream")

    async def _submit_prompt(self, text: str) -> None:
        # Include disabled buttons in the selector set so a still-processing
        # upload waits for readiness instead of being mistaken for no button.
        selectors = tuple(sel.replace(":not(.lv-btn-disabled)", "") for sel in S.SUBMIT)
        await submit_prompt(
            self._page, text, S.COMPOSER, selectors, name="Seedream",
            allow_enter=False, accepted_url_fragment="/ai-tool/generate",
        )

    # ── The public video call ───────────────────────────────────

    async def generate_video(
        self,
        *,
        prompt: str,
        mode: str = M.MODE_TEXT,
        model_id: str = "",
        image_paths: "list[str] | None" = None,
        duration_s: "float | None" = None,
        aspect_ratio: str = "",
        resolution: str = "",
        audio: "bool | None" = None,
    ) -> ChatResponse:
        """Drive one Dreamina video generation and return the finished clip.

        `image_paths` are the already-downloaded local files to upload, in the
        order the mode expects (first,last / keyframes / references). Selection of
        model/aspect/resolution/duration/ref-mode is best-effort."""
        start = time.time()
        model_id = model_id or M.DEFAULT_MODEL_ID
        image_paths = image_paths or []

        # 1. Fresh generate view. The worker pool has already done this when a
        # request-wide new-chat/temporary policy is active.
        if not fresh_chat_required():
            await self.new_chat()

        # 2. Controls (best-effort; UI is the authority).
        await self._set_model(model_id)
        if aspect_ratio:
            await self._set_aspect(aspect_ratio)
        if resolution:
            await self._set_resolution(resolution)
        if duration_s:
            await self._set_duration(duration_s)
        if mode in M.MODE_TO_REF_MODE:
            await self._set_ref_mode(mode)

        # 3. Upload images for image-based modes.
        if image_paths:
            await self._upload(image_paths)

        # 4. Prompt.
        await self._type_prompt(prompt or "")

        # 5. Submit, then wait for the app to enter the generate/results view.
        await self._submit_prompt(prompt or "")
        log.info(
            f"Seedream: submitted (model={model_id}, mode={mode}, "
            f"{len(image_paths)} image(s))"
        )
        try:
            await self._page.wait_for_url(S.GENERATE_URL_GLOB, timeout=30000)
        except Exception:
            log.debug("Seedream: generate-view URL not observed (may already be there)")

        # Snapshot the baseline HERE — on the results view we actually poll, AFTER
        # navigation. Capturing it on the home/composer view would diff two
        # different documents, so a reused tab's PRIOR finished clips (rendered in
        # the results grid but absent from the home snapshot) would be accepted as
        # this request's result. Our new clip shows the capcutstatic spinner first
        # (excluded), so anything already-finished here is genuinely prior work.
        baseline = await capture_video_srcs(self._page)

        # 6. Poll for the finished video.
        limit_hit = False
        limit_reset = 0.0
        src = None
        message = ""
        try:
            src = await wait_for_new_video(
                self._page,
                baseline=baseline,
                timeout_ms=Config.VIDEO_RESPONSE_TIMEOUT,
            )
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("credit:"):
                limit_hit = True
                limit_reset = 0.0
                message = msg[len("credit:"):].strip()
                log.warning(f"Seedream: credit/paywall limit — {message}")
            else:
                message = msg[len("failed:"):].strip() if msg.startswith("failed:") else msg
                log.warning(f"Seedream: generation failed — {message}")

        # 7. Download.
        videos: list = []
        if src:
            alt = (prompt or "seedream")[:60]
            local = await extract_video(self._page, src, alt=alt)
            if local:
                videos.append(VideoInfo(
                    url=src if not src.startswith("blob:") else "",
                    alt=alt, local_path=local, prompt_title=alt,
                    mime_type="video/webm" if local.endswith(".webm") else "video/mp4",
                    duration_s=float(duration_s or 0.0),
                ))

        self.last_video_model = model_id if videos else ""
        elapsed_ms = int((time.time() - start) * 1000)
        if not message:
            message = "" if videos else "Seedream produced no video before the timeout."

        log.info(
            f"Seedream done ({elapsed_ms}ms, {len(videos)} video(s)"
            f"{', limit' if limit_hit else ''})"
        )
        return ChatResponse(
            message=message,
            thread_id="",
            response_time_ms=elapsed_ms,
            videos=videos,
            has_videos=bool(videos),
            limit_hit=limit_hit,
            limit_kind="credit" if limit_hit else "",
            limit_reset_seconds=limit_reset,
        )

    # ── The public image call (via send_message) ────────────────

    async def _generate_image(
        self,
        *,
        prompt: str,
        image_paths: "list[str] | None" = None,
        image_model: str = "",
    ) -> ChatResponse:
        """Drive one Dreamina image generation and return the finished image(s).

        Reference images (if any) drive image-to-image / multi-image fusion.
        Model selection is best-effort — the live UI is the authority."""
        start = time.time()
        model_id = M.resolve_image_model_id(image_model)
        image_paths = image_paths or []

        # 1. Fresh image-generate view.
        await self._goto_image_home()

        # 2. Model (best-effort — try the label aliases for this slug).
        labels = M.image_label_candidates(model_id)
        if labels and await self._select_dropdown_option(labels):
            log.info(f"Seedream: selected image model '{labels[0]}'")
        else:
            log.info(f"Seedream: image model '{model_id}' not offered — keeping current")

        # 3. Reference images (image-to-image / fusion), if any.
        if image_paths:
            await self._upload(image_paths)

        # 4. Prompt.
        await self._type_prompt(prompt or "")

        # 5. Submit, wait for the results view, THEN snapshot the baseline on the
        #    view we poll (same reasoning as the video path — avoids accepting a
        #    reused tab's prior images).
        await self._submit_prompt(prompt or "")
        log.info(f"Seedream: image submitted (model={model_id}, {len(image_paths)} ref(s))")
        try:
            await self._page.wait_for_url(S.GENERATE_URL_GLOB, timeout=30000)
        except Exception:
            log.debug("Seedream: generate-view URL not observed (may render in-place)")
        baseline = await capture_image_srcs(self._page)

        # 6. Poll for finished images (Dreamina streams up to 4 variants).
        limit_hit = False
        message = ""
        srcs: list = []
        try:
            srcs = await wait_for_new_images(
                self._page, baseline=baseline, timeout_ms=Config.RESPONSE_TIMEOUT,
            )
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("credit:"):
                limit_hit = True
                message = msg[len("credit:"):].strip()
                log.warning(f"Seedream: credit/paywall limit — {message}")
            else:
                message = msg[len("failed:"):].strip() if msg.startswith("failed:") else msg
                log.warning(f"Seedream: image generation failed — {message}")

        # 7. Download each.
        images: list = []
        alt = (prompt or "seedream")[:60]
        for src in srcs:
            local = await extract_image(self._page, src, alt=alt)
            if local:
                images.append(ImageInfo(
                    url=src if not src.startswith("blob:") else "",
                    alt=alt, local_path=local, prompt_title=alt,
                ))

        self.last_image_model = model_id if images else ""
        elapsed_ms = int((time.time() - start) * 1000)
        if not message:
            message = "" if images else "Seedream produced no image before the timeout."

        log.info(
            f"Seedream image done ({elapsed_ms}ms, {len(images)} image(s)"
            f"{', limit' if limit_hit else ''})"
        )
        return ChatResponse(
            message=message,
            thread_id="",
            response_time_ms=elapsed_ms,
            images=images,
            has_images=bool(images),
            limit_hit=limit_hit,
            limit_kind="credit" if limit_hit else "",
            limit_reset_seconds=0.0,
        )
