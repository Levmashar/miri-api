"""
Shared browser client for TEXT-ONLY chat providers (DeepSeek, Qwen).

It implements exactly the interface the Worker/pool machinery calls — the same
one ChatGPTClient/GeminiClient/GrokClient expose:

    send_message(text, image_paths=, file_paths=, expect_image=, image_model=,
                 chat_model=) -> ChatResponse
    new_chat(), navigate_to_thread(id), _extract_thread_id(), list_threads()

The Gemini and Grok clients are hand-written because each has its own image
extraction and per-provider quirks. DeepSeek and Qwen differ from each other in
nothing but selectors, URLs and their model registries, so they subclass this
instead of being two copies of the same 300 lines. A subclass sets the class
attributes below; anything genuinely provider-specific is a normal override.

The typing/sending mechanics follow what the other clients learned the hard way:
focus and click through JS inside page.evaluate() rather than element.click(),
because Playwright's actionability + scroll-into-view can hang for 30 seconds on
these composers.
"""

from __future__ import annotations

import asyncio
import re
import time

from src.accounts.limit_detector import detect_limit
from src.core.browser.human import random_delay
from src.core.config import Config
from src.providers.temporary_chat import temporary_new_chat, verify_temporary_chat
from src.providers.chatgpt.models import ChatResponse
from src.providers.model_picker import apply_modes, pick_from_dropdown
from src.providers.composer import enter_prompt, submit_prompt

_JS_CLICK = """
(sels) => {
    for (const s of sels) {
        let b = null;
        try { b = document.querySelector(s); } catch (err) { continue; }
        if (!b || b.disabled) continue;
        if (b.getAttribute && b.getAttribute('aria-disabled') === 'true') continue;
        const shown = b.offsetParent !== null ||
                      !!(b.getClientRects && b.getClientRects().length);
        if (!shown) continue;
        b.click();
        return true;
    }
    return false;
}
"""


class TextChatClient:
    """One tab of a text-only chat provider."""

    # ── Subclass contract ───────────────────────────────────────
    NAME = "provider"          # label used in logs
    URL = ""                   # where a fresh tab starts / new_chat() goes
    THREAD_URL_TEMPLATE = ""   # e.g. "https://chat.example.com/c/{thread_id}"
    THREAD_RE: "re.Pattern | None" = None   # pulls the thread id out of the URL
    SELECTORS = None           # the provider's selectors class
    MODELS = None              # the provider's chat-model registry module
    DETECTOR = None            # the provider's detector module (count/wait/extract)
    LOG = None                 # a logger from src.core.log.setup_logging

    def __init__(self, page) -> None:
        self._page = page
        # Which chat model/mode the LAST send actually ran on ("" = whatever the
        # account already had selected). Providers here generate no images.
        self.last_chat_model = ""
        self.last_image_model = ""
        # Composer modes this tab currently has switched on. The UI keeps them
        # between requests, so the next request has to be able to clear them.
        self._modes_on: list = []
        # False until this client has reconciled the UI's switches once — see
        # _sync_state() below.
        self._modes_synced = False

    @property
    def page(self):
        return self._page

    # ── Helpers ─────────────────────────────────────────────────

    def _extract_thread_id(self) -> str:
        """Thread id from the URL, '' on a fresh chat page."""
        if self.THREAD_RE is None:
            return ""
        m = self.THREAD_RE.search(self._page.url or "")
        return m.group(1) if m else ""

    async def _count_turns(self) -> int:
        return await self.DETECTOR.count_assistant_turns(self._page)

    async def _enter_prompt(self, text: str) -> None:
        await enter_prompt(self._page, text, self.SELECTORS.CHAT_INPUT, name=self.NAME)

    async def _submit_prompt(self, text: str) -> None:
        await submit_prompt(
            self._page, text, self.SELECTORS.CHAT_INPUT, self.SELECTORS.SEND_BUTTON,
            name=self.NAME, user_selectors=getattr(self.SELECTORS, 'USER_TURN', ()),
            stop_selectors=getattr(self.SELECTORS, 'STOP_BUTTON', ()),
        )

    async def _read_text_response(self, pre_count: int, timeout_ms: int) -> str:
        ok = await self.DETECTOR.wait_for_response_complete(
            self._page, pre_count=pre_count, timeout_ms=timeout_ms
        )
        if not ok:
            raise RuntimeError(f"{self.NAME}: no complete new text response before timeout")
        text = await self.DETECTOR.extract_latest_response(self._page)
        if not text.strip():
            raise RuntimeError(f"{self.NAME}: new text response was empty")
        return text

    # ── Model selection ─────────────────────────────────────────

    async def _select_chat_model(self, chat_model: str) -> str:
        """Point the UI at the requested chat model / mode before sending.

        Returns the id actually applied, "" when nothing was changed — an
        unknown id, the provider's default id, or a model this account is not
        offered all mean "answer on whatever the UI already has" rather than
        fail a request the account could have served.
        """
        M, S, log = self.MODELS, self.SELECTORS, self.LOG
        wanted = M.resolve_chat_model_id(chat_model)
        self.last_chat_model = ""
        active, assume_on = self._suspect_modes()

        if not wanted or wanted == M.DEFAULT_MODEL_ID:
            # A default request must not inherit a mode from the last one.
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
                    what=f"{self.NAME} mode",
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
                what=f"{self.NAME} model {wanted}",
            ))

        # One switch per group ("DeepThink + Search" needs both), and any mode a
        # previous request on this tab turned on is switched back off — these
        # toggles survive between requests in the UI.
        enabled, self._modes_on = await apply_modes(
            self._page,
            wanted_groups=M.tool_groups(wanted),
            active_groups=active,
            assume_on=assume_on,
            button_selectors=S.MODE_BUTTON,
            menu_selectors=S.MODE_MENU_BUTTON,
            menu_item_selectors=S.MODE_BUTTON,
            log=log,
            what=f"{self.NAME} mode {wanted}",
        )
        applied = applied or enabled

        self.last_chat_model = wanted if applied else ""
        return self.last_chat_model

    def _suspect_modes(self) -> "tuple[list, bool]":
        """(groups that may be switched on, whether we know they are).

        A tab's switches survive between requests AND between sessions — the
        browser profile is persistent, so a mode switched on days ago is still
        on while a freshly created client remembers nothing. So the FIRST
        selection on a tab treats every switch the registry knows as suspect,
        with assume_on=False: only a switch the page positively reports as on
        gets cleared, never a blind click. After that we go by what we set.
        """
        if self._modes_synced:
            return self._modes_on, True
        self._modes_synced = True
        return self.MODELS.all_tool_groups(), False

    # ── Sending ─────────────────────────────────────────────────

    async def send_message(
        self,
        text: str,
        image_paths: list | None = None,
        file_paths: list | None = None,
        expect_image: bool = False,
        image_model: str = "",
        chat_model: str = "",
    ) -> ChatResponse:
        S, M, log = self.SELECTORS, self.MODELS, self.LOG
        start = time.time()
        attachments = (image_paths or []) + (file_paths or [])

        # Model choice BEFORE typing — switching re-renders the composer and can
        # drop a half-typed prompt.
        wanted_model = await self._select_chat_model(chat_model)
        await verify_temporary_chat(self._page)

        # Count the turns AFTER the model choice: picking a model can reset or
        # re-render the conversation, and a pre_count taken before that would
        # never be exceeded — the completion wait would sit there until timeout.
        pre_count = await self._count_turns()

        if attachments:
            await self._upload(attachments)

        await self._enter_prompt(text)

        await self._submit_prompt(text)

        log.info(
            f"{self.NAME}: sent ({len(text)} chars, {len(attachments)} attachment(s)"
            f"{f', model={wanted_model}' if wanted_model else ''})"
        )

        # Reasoning / research modes run for minutes; they get the long deadline.
        timeout_ms = (
            Config.LONG_MODE_TIMEOUT if M.is_long(wanted_model) else Config.RESPONSE_TIMEOUT
        )
        response_text = await self._read_text_response(pre_count, timeout_ms)

        elapsed_ms = int((time.time() - start) * 1000)

        # Usage-limit banner detection — same contract as the other clients, so
        # the account layer can cool this account down and reroute.
        limit = detect_limit(response_text)
        if limit.hit:
            log.warning(f"{self.NAME}: usage-limit banner detected: {response_text[:120]}")

        log.info(f"{self.NAME} response ({elapsed_ms}ms, {len(response_text)} chars)")

        return ChatResponse(
            message=response_text,
            thread_id=self._extract_thread_id(),
            response_time_ms=elapsed_ms,
            limit_hit=limit.hit,
            limit_kind=limit.kind,
            limit_reset_seconds=limit.reset_seconds,
        )

    async def _upload(self, paths: list) -> None:
        """Attach files via the hidden <input type=file>."""
        S, log = self.SELECTORS, self.LOG
        try:
            inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                await self._page.evaluate(_JS_CLICK, list(S.UPLOAD_BUTTON))
                await random_delay(400, 800)
                inp = await self._page.query_selector("input[type='file']")
            if inp is None:
                raise RuntimeError(f"{self.NAME}: no file input found; attachments were not uploaded")
            await inp.set_input_files(paths)
            await asyncio.sleep(2 + 1.0 * len(paths))
            log.info(f"{self.NAME}: attached {len(paths)} file(s)")
        except Exception as e:
            raise RuntimeError(f"{self.NAME}: attachment upload failed") from e

    # ── Navigation ──────────────────────────────────────────────

    @temporary_new_chat
    async def new_chat(self) -> None:
        try:
            await self._page.goto(self.URL, wait_until="domcontentloaded")
            await random_delay(700, 1300)
            self.LOG.info(f"{self.NAME}: new chat")
        except Exception as e:
            self.LOG.error(f"{self.NAME}: new_chat failed: {e}")
            raise

    async def navigate_to_thread(self, thread_id: str) -> None:
        """Open an existing conversation and wait for its turns to render.

        The wait matters: send_message counts existing turns straight after
        this, and counting 0 on a half-loaded page would make the detector think
        the reply already arrived and extract a STALE answer.
        """
        log = self.LOG
        url = self.THREAD_URL_TEMPLATE.format(thread_id=thread_id)
        log.info(f"{self.NAME}: navigating to thread {thread_id}")
        await self._page.goto(url, wait_until="domcontentloaded")
        prev = -1
        for _ in range(40):  # up to ~8s for the turns to render and settle
            await asyncio.sleep(0.2)
            count = await self._count_turns()
            if count == prev and count > 0:
                break
            prev = count
        if prev <= 0:
            log.warning(f"{self.NAME}: thread {thread_id} rendered no turns")
        else:
            log.info(f"{self.NAME}: thread {thread_id} loaded ({prev} turn(s))")

    async def get_current_thread_url(self) -> str:
        return self._page.url

    async def list_threads(self) -> list:
        """Recent conversations from the side nav (best-effort)."""
        S = self.SELECTORS
        if not getattr(S, "SIDEBAR_THREAD_LINKS", None):
            return []
        try:
            return await self._page.evaluate(
                """
                (sels) => {
                    let links = [];
                    for (const s of sels) {
                        try { links = Array.from(document.querySelectorAll(s)); }
                        catch (e) { continue; }
                        if (links.length) break;
                    }
                    return links.slice(0, 20).map((a, i) => ({
                        id: (a.getAttribute('href') || '').split('/').filter(Boolean).pop() || String(i),
                        title: (a.innerText || '').trim().slice(0, 80),
                        url: a.href || '',
                    }));
                }
                """,
                list(S.SIDEBAR_THREAD_LINKS),
            ) or []
        except Exception:
            return []
