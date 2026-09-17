"""
DeepSeek response-completion detector.

A thin binding of the shared DOM detector (src/providers/dom_detector.py) to
DeepSeek's selectors: a new assistant turn must appear, then its text must stop
changing. Like Gemini/Grok — and unlike ChatGPT/Claude — the answer is read
straight from the DOM, so DeepSeek tabs never contend on the X11 clipboard lock.
"""

from __future__ import annotations

from src.core.log import setup_logging
from src.providers import dom_detector
from src.providers.deepseek.selectors import DeepSeekSelectors as S

log = setup_logging("deepseek_detector")

NAME = "DeepSeek"


async def count_assistant_turns(page) -> int:
    return await dom_detector.count_assistant_turns(page, S.ASSISTANT_TURN, S.USER_TURN)


async def latest_response_text(page) -> str:
    return await dom_detector.latest_response_text(page, S.ASSISTANT_TURN, S.USER_TURN)


async def wait_for_response_complete(page, *, pre_count: int, timeout_ms: int,
                                     **kwargs) -> bool:
    return await dom_detector.wait_for_response_complete(
        page,
        turn_selectors=S.ASSISTANT_TURN,
        user_selector=S.USER_TURN,
        pre_count=pre_count,
        timeout_ms=timeout_ms,
        log=log,
        name=NAME,
        **kwargs,
    )


async def extract_latest_response(page) -> str:
    return await dom_detector.extract_latest_response(
        page, S.ASSISTANT_TURN, S.USER_TURN, log=log, name=NAME
    )
