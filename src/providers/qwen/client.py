"""
Qwen web client — drives chat.qwen.ai (Alibaba's Tongyi Qianwen).

Chat mechanics live in TextChatClient (src/providers/text_client.py); this file
binds them to Qwen and adds what only Qwen has among the text providers: IMAGE
and VIDEO generation.

Qwen generates media from the same composer as chat, switched into a dedicated
mode — "Image Generation", "Image Edit" or "Video Generation" (see the media
section of src/providers/qwen/models.py). One media request is:

  1. a fresh chat (a clean thread, so the result is unambiguous),
  2. the mode switch turned on (and any leftover chat mode turned off),
  3. references uploaded, aspect ratio picked (video),
  4. a baseline of every image/video src on the page, then send,
  5. poll for a NEW src that isn't the user's upload, download it,
  6. the media mode switched back off, so the next chat request is chat.

Media tool selection and submission must be confirmed before waiting for a
result. Missing tools or an unacknowledged send raise an error immediately.

Thread URL: https://chat.qwen.ai/c/<uuid>
"""

from __future__ import annotations

import re
import asyncio
import time

from src.accounts.limit_detector import LimitInfo
from src.core.config import Config
from src.providers.temporary_chat import fresh_chat_required, verify_temporary_chat
from src.core.log import setup_logging
from src.providers.chatgpt.models import ChatResponse, ImageInfo, VideoInfo
from src.providers.media_fetch import download_media
from src.providers.model_picker import apply_modes, pick_from_dropdown, set_mode
from src.providers.qwen import detector as DET
from src.providers.qwen import models as M
from src.providers.qwen.selectors import QwenSelectors as S
from src.providers.qwen.text_capture import QwenTextCapture
from src.providers.qwen.media_capture import QwenMediaCapture
from src.providers.text_client import TextChatClient
from src.providers.qwen.media_mode import select_media_tool, clear_media_tool

log = setup_logging("qwen_client")

QWEN_URL = "https://chat.qwen.ai"


class QwenClient(TextChatClient):
    """One Qwen tab — chat, image generation and video generation."""

    NAME = "Qwen"
    URL = QWEN_URL
    THREAD_URL_TEMPLATE = QWEN_URL + "/c/{thread_id}"
    THREAD_RE = re.compile(r"/c/([A-Za-z0-9_-]+)")
    SELECTORS = S
    MODELS = M
    DETECTOR = DET
    LOG = log

    def __init__(self, page) -> None:
        super().__init__(page)
        self.last_video_model = ""

    # ── Entry points ────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list | None = None,
        file_paths: list | None = None,
        expect_image: bool = False,
        image_model: str = "",
        chat_model: str = "",
    ) -> ChatResponse:
        """Chat, or — when the caller expects an image — image generation.

        /v1/images/* and the Responses API's image_generation tool reach Qwen
        through here with expect_image=True, exactly as they reach Gemini/Grok.
        """
        if expect_image:
            return await self.generate_image(
                prompt=text, image_paths=image_paths, image_model=image_model
            )
        # Reconcile the UI again after media generation (including failed mode
        # cleanup). Otherwise _modes_on=[] could hide a still-enabled media mode.
        self._modes_synced = False
        await clear_media_tool(self._page)
        async with QwenTextCapture(self._page, text) as capture:
            self._text_capture = capture
            try:
                return await super().send_message(
                    text, image_paths=image_paths, file_paths=file_paths,
                    image_model=image_model, chat_model=chat_model,
                )
            finally:
                self._text_capture = None

    async def _read_text_response(self, pre_count: int, timeout_ms: int) -> str:
        capture = getattr(self, '_text_capture', None)
        if capture is None:
            return await super()._read_text_response(pre_count, timeout_ms)
        dom_task = asyncio.create_task(super()._read_text_response(pre_count, timeout_ms))
        network_task = asyncio.create_task(capture.wait())
        pending = {dom_task, network_task}
        deadline = time.monotonic() + timeout_ms / 1000
        last_error = None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, timeout=max(0, deadline - time.monotonic()),
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break
                for task in done:
                    try:
                        text = task.result()
                        if text.strip():
                            source = 'browser response' if task is network_task else 'DOM'
                            log.info(f'Qwen: completed text via {source} ({len(text)} chars)')
                            return text
                    except Exception as error:
                        last_error = error
                # If DOM has exhausted its deadline, do not wait another full
                # timeout for the network capture. Both share the same budget.
                if dom_task.done() and not capture.requests:
                    break
            log.warning(f'Qwen text timeout: network={capture.diagnostics()}')
            raise last_error or RuntimeError('Qwen: no complete new text response before timeout')
        finally:
            for task in (dom_task, network_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(dom_task, network_task, return_exceptions=True)

    async def generate_image(
        self,
        *,
        prompt: str,
        image_paths: "list[str] | None" = None,
        image_model: str = "",
    ) -> ChatResponse:
        """Text -> image, or image + text -> image when references are attached.

        Attached references always mean Image Edit (that is what the mode is
        for), whatever image model id the caller named; without references it is
        Image Generation. If the account doesn't offer Image Edit, generation
        with the references attached is the fallback.
        """
        start = time.time()
        image_paths = image_paths or []
        edit = bool(image_paths)
        requested = M.resolve_image_model_id(image_model)
        modes = [M.IMAGE_EDIT_MODE, M.IMAGE_GEN_MODE] if edit else [M.IMAGE_GEN_MODE]
        if requested == "qwen-image-edit" and not edit:
            log.info("Qwen: qwen-image-edit without a reference image — generating instead")

        alt = (prompt or "qwen image")[:60]
        items, message, limit, used_mode = await self._run_media(
            kind="image",
            mode_candidates=modes,
            prompt=prompt,
            image_paths=image_paths,
            aspect_ratio="",
            timeout_ms=Config.IMAGE_RESPONSE_TIMEOUT,
            settle_s=6.0,
            alt=alt,
        )
        images = [
            ImageInfo(url=src if src.startswith("http") else "", alt=alt,
                      local_path=path, prompt_title=alt)
            for src, path, _mime in items
        ]

        ran = "qwen-image-edit" if used_mode == M.IMAGE_EDIT_MODE else "qwen-image"
        self.last_image_model = ran if images else ""
        return self._media_response(
            start, message, limit, images=images, kind="image", model=ran
        )

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
        """Text -> video, or image -> video from ONE start image.

        Same signature as SeedreamClient.generate_video, so the video endpoint
        drives both the same way. Qwen's video mode exposes no duration,
        resolution or audio controls, so those are accepted and reported as
        ignored rather than silently pretended.
        """
        start = time.time()
        model_id = M.resolve_video_model_id(model_id)
        if mode not in M.VIDEO_MODES:
            raise RuntimeError(
                f"Qwen video supports {', '.join(M.VIDEO_MODES)} — not {mode!r}"
            )
        refs = (image_paths or [])[:1] if mode == M.MODE_I2V else []
        ignored = [n for n, v in (("duration_s", duration_s), ("resolution", resolution),
                                  ("audio", audio)) if v not in (None, "", False)]
        if ignored:
            log.info(f"Qwen: video mode has no control for {', '.join(ignored)} — ignored")

        alt = (prompt or "qwen video")[:60]
        items, message, limit, _used = await self._run_media(
            kind="video",
            mode_candidates=[M.VIDEO_GEN_MODE],
            prompt=prompt,
            image_paths=refs,
            aspect_ratio=aspect_ratio,
            timeout_ms=Config.VIDEO_RESPONSE_TIMEOUT,
            settle_s=3.0,
            alt=alt,
            max_items=1,  # one clip per request
        )
        videos = [
            VideoInfo(url=src if src.startswith("http") else "", alt=alt,
                      local_path=path, prompt_title=alt, mime_type=mime or "video/mp4")
            for src, path, mime in items
        ]

        self.last_video_model = model_id if videos else ""
        return self._media_response(
            start, message, limit, videos=videos, kind="video", model=model_id
        )

    # ── The shared media pipeline ───────────────────────────────

    async def _run_media(
        self,
        *,
        kind: str,
        mode_candidates: list,
        prompt: str,
        image_paths: list,
        aspect_ratio: str,
        timeout_ms: int,
        settle_s: float,
        alt: str,
        max_items: "int | None" = None,
    ) -> "tuple[list, str, LimitInfo, tuple | None]":
        """Drive one media generation.

        Returns (items, message, limit, mode_used), items being (src, local_path,
        mime) for each result actually saved. Downloading happens HERE, before
        the media mode is switched back off: a blob: result is only readable
        while the page that made it still holds it, so nothing that could
        re-render the thread runs until the bytes are on disk.
        """
        # The worker pool has already opened the request's fresh chat when a
        # request-wide new-chat/temporary policy is active. Re-opening here
        # would create two temporary chats for one media request.
        if not fresh_chat_required():
            await self.new_chat()

        used_mode = None
        try:
            used_mode = await self._enter_media_mode(mode_candidates, kind)
            await verify_temporary_chat(self._page)
            if aspect_ratio:
                await self._set_aspect(aspect_ratio)
            if image_paths:
                await self._upload(image_paths)

            # Baseline AFTER the upload: the composer's preview of a reference
            # image is then already known and can't be taken for the result.
            baseline = await DET.capture_media_srcs(self._page, kind)

            async with QwenMediaCapture(self._page, prompt or "", kind) as capture:
                await self._enter_prompt(prompt or "")
                await self._submit_prompt(prompt or "")
                log.info(
                    f"Qwen: {kind} request sent ({len(prompt or '')} chars, "
                    f"{len(image_paths)} reference(s), mode={used_mode[0] if used_mode else 'none'})"
                )

                message, limit = "", LimitInfo(hit=False)
                try:
                    srcs = await DET.wait_for_new_media(
                        self._page, kind=kind, baseline=baseline,
                        timeout_ms=timeout_ms, settle_s=settle_s, capture=capture,
                    )
                except RuntimeError as e:
                    srcs, text = [], str(e)
                    if text.startswith("limit:"):
                        message = text[len("limit:"):].strip()
                        limit = LimitInfo(hit=True, kind=kind, raw=message)
                        log.warning(f"Qwen: usage limit during {kind} generation — {message[:120]}")
                    else:
                        message = text[len("failed:"):].strip() if text.startswith("failed:") else text
                        log.warning(f"Qwen: {kind} generation failed — {message[:120]}")

                items = []
                for src in srcs:
                    path, mime = await download_media(
                        self._page, src, kind=kind, alt=alt, log=log, name=self.NAME
                    )
                    if path:
                        items.append((src, path, mime))
                        if max_items and len(items) >= max_items:
                            break

                if not items and not message:
                    if srcs:
                        # Say plainly that it was made but not saved — not "no media".
                        message = f"Qwen produced a {kind} but it could not be downloaded."
                    else:
                        # Surface whatever Qwen said instead (a refusal, a question).
                        message = await DET.latest_response_text(self._page) or (
                            f"Qwen produced no {kind} before the timeout."
                        )
                return items, message, limit, used_mode
        finally:
            await self._leave_media_mode(used_mode, kind)
            self._modes_synced = False

    async def _enter_media_mode(self, candidates: list, kind: str) -> "tuple | None":
        """Switch the composer into the first media mode this account offers.

        Also clears whatever chat mode (Thinking, Search) the tab had on, so
        "generate an image" isn't run as a reasoning or search request.
        """
        # A fresh page can preserve settings from the account's last session.
        await clear_media_tool(self._page)
        self._modes_synced = False
        active, assume_on = self._suspect_modes()
        await apply_modes(
            self._page, wanted_groups=[],
            active_groups=[g for g in active if tuple(g) not in map(tuple, candidates)],
            assume_on=assume_on, button_selectors=S.MEDIA_TOOL,
            menu_selectors=S.MODE_MENU_BUTTON, menu_item_selectors=S.MEDIA_TOOL,
            log=log, what=f"Qwen {kind} mode",
        )
        self._modes_on = []
        for group in candidates:
            if await select_media_tool(self._page, group):
                log.info(f"Qwen: confirmed {group[0]} tool selected")
                return tuple(group)
        raise RuntimeError(f"Qwen: could not select and verify the {kind} generation tool; prompt was not sent")

    async def _leave_media_mode(self, mode: "tuple | None", kind: str) -> None:
        """Switch the media mode back off, so the tab's next request is chat.

        assume_on=False: if the page won't say whether the mode is on, leave it
        rather than risk clicking it back ON. The next chat request's first-sync
        pass covers the rest.
        """
        if not mode:
            return
        try:
            await clear_media_tool(self._page)
            await set_mode(
                self._page,
                labels=list(mode),
                button_selectors=S.MEDIA_TOOL,
                desired=False,
                assume_on=False,
                log=log,
                what=f"Qwen {kind} mode",
            )
        except Exception as e:
            log.debug(f"Qwen: leaving {kind} mode failed: {e}")

    async def _set_aspect(self, aspect_ratio: str) -> bool:
        """Pick an aspect ratio in the media composer. Best-effort."""
        wanted = (aspect_ratio or "").strip()
        if wanted not in M.ASPECT_RATIOS:
            log.info(f"Qwen: aspect ratio {wanted!r} not offered "
                     f"({', '.join(M.ASPECT_RATIOS)}) — using the UI default")
            return False
        return bool(await pick_from_dropdown(
            self._page,
            open_selectors=S.ASPECT_MENU_BUTTON,
            item_selectors=S.ASPECT_MENU_ITEM,
            labels=[wanted],
            log=log,
            what="Qwen aspect ratio",
        ))

    def _media_response(self, start: float, message: str, limit: LimitInfo, *,
                        kind: str, model: str, images=None, videos=None) -> ChatResponse:
        images, videos = images or [], videos or []
        produced = len(images) + len(videos)
        elapsed_ms = int((time.time() - start) * 1000)
        log.info(
            f"Qwen {kind} done ({elapsed_ms}ms, {produced} {kind}(s), model={model}"
            f"{', limit' if limit.hit else ''})"
        )
        return ChatResponse(
            message="" if produced else message,
            thread_id=self._extract_thread_id(),
            response_time_ms=elapsed_ms,
            images=images,
            has_images=bool(images),
            videos=videos,
            has_videos=bool(videos),
            limit_hit=limit.hit,
            limit_kind=limit.kind,
            limit_reset_seconds=limit.reset_seconds,
        )
