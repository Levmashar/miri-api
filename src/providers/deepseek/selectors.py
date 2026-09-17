"""
DeepSeek (chat.deepseek.com) selectors.

DeepSeek's app is a React build with hashed class names, so anything derived
from those would rot on the next deploy. Two things are stable and are what
these selectors lean on:

  * `#chat-input` — the composer textarea keeps its id across releases.
  * `.ds-markdown` — the rendered answer body carries DeepSeek's own design-system
    class ("ds-" = DeepSeek design system), which is not a build hash.

Everything else is a loose fallback, and the model/mode switches ("DeepThink",
"Search") are matched by their visible TEXT rather than by markup — see
src/providers/model_picker.py. Assistant turns are additionally filtered
NEGATIVELY (a node that sits inside a user turn is not an answer), which holds
whatever DeepSeek renames next.

Thread URL: https://chat.deepseek.com/a/chat/s/<uuid>
"""

from __future__ import annotations


class DeepSeekSelectors:
    # ── Composer ────────────────────────────────────────────────
    CHAT_INPUT = [
        "textarea#chat-input",
        "#chat-input",
        "textarea[placeholder*='message' i]",
        "textarea",
        "div[contenteditable='true']",
    ]

    SEND_BUTTON = [
        "div[role='button'][aria-disabled='false']:has(svg)",
        "button[type='submit']",
        "button[aria-label*='send' i]",
        "div[role='button'][aria-label*='send' i]",
    ]

    STOP_BUTTON = [
        "div[role='button'][aria-label*='stop' i]",
        "button[aria-label*='stop' i]",
    ]

    # ── Conversation turns ──────────────────────────────────────
    # The answer body first, then progressively looser containers.
    ASSISTANT_TURN = [
        "div.ds-markdown",
        "[class*='ds-markdown']",
        "div[class*='_4f9bf79']",
        "[class*='message'][class*='assistant']",
    ]
    # Anything inside a user turn is not an answer (negative filter).
    USER_TURN = "[class*='_9663006'], [class*='user-message'], [class*='fbb737a4']"

    # ── Login state ─────────────────────────────────────────────
    # DeepSeek requires an account to chat at all, so a visible sign-in
    # affordance is the decisive "not signed in" signal.
    LOGIN_INDICATORS = [
        "button:has-text('Log in')",
        "a:has-text('Log in')",
        "button:has-text('Sign in')",
        "input[placeholder*='phone number' i]",
        "input[placeholder*='email' i][type='text']",
    ]
    # The composer only renders for a signed-in session.
    LOGGED_IN_INDICATORS = [
        "textarea#chat-input",
        "[class*='ds-avatar']",
    ]

    # ── Attachments ─────────────────────────────────────────────
    FILE_INPUT = ["input[type='file']"]
    UPLOAD_BUTTON = [
        "input[type='file']",
        "div[role='button'][aria-label*='upload' i]",
        "button[aria-label*='attach' i]",
    ]

    # ── Model picker + modes ────────────────────────────────────
    # DeepSeek has no model dropdown: the choice IS the two composer switches,
    # "DeepThink" (the reasoning model) and "Search" (web search), which can be
    # on at the same time. MODEL_MENU_* are kept empty so the shared picker
    # skips the dropdown step entirely.
    MODEL_MENU_BUTTON: list = []
    MODEL_MENU_ITEM: list = []
    # Selector ORDER is priority (see src/providers/model_picker.py), so the
    # composer's own controls are listed before any page-wide fallback. Without
    # that, the sidebar's "Search chats" control would answer to the label
    # "Search" and get clicked instead of the composer's Search switch.
    MODE_BUTTON = [
        "form div[role='button']",
        "footer div[role='button']",
        "div[role='button']",
        "button",
    ]
    MODE_MENU_BUTTON: list = []

    # ── Sidebar ─────────────────────────────────────────────────
    SIDEBAR_THREAD_LINKS = [
        "a[href^='/a/chat/s/']",
    ]

    NEW_CHAT_BUTTON = [
        "div[role='button'][aria-label*='new chat' i]",
        "a[href='/a/chat']",
    ]
