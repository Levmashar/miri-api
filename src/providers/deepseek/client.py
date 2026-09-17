"""
DeepSeek web client — drives chat.deepseek.com.

All of the mechanics live in TextChatClient (src/providers/text_client.py); this
file is the provider's binding: where its tabs live, how a thread id is read out
of the URL, and which selectors / model registry to use.

Model choice on DeepSeek is not a dropdown but two composer switches, DeepThink
and Search. The shared picker turns them on and — because they are sticky per
tab — back off again. See src/providers/deepseek/models.py.

Thread URL: https://chat.deepseek.com/a/chat/s/<uuid>
"""

from __future__ import annotations

import re

from src.core.log import setup_logging
from src.providers.deepseek import detector as DET
from src.providers.deepseek import models as M
from src.providers.deepseek.selectors import DeepSeekSelectors as S
from src.providers.text_client import TextChatClient

log = setup_logging("deepseek_client")

DEEPSEEK_URL = "https://chat.deepseek.com"


class DeepSeekClient(TextChatClient):
    """One DeepSeek tab."""

    NAME = "DeepSeek"
    URL = DEEPSEEK_URL
    THREAD_URL_TEMPLATE = DEEPSEEK_URL + "/a/chat/s/{thread_id}"
    THREAD_RE = re.compile(r"/a/chat/s/([A-Za-z0-9_-]+)")
    SELECTORS = S
    MODELS = M
    DETECTOR = DET
    LOG = log
