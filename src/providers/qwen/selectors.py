"""
Qwen (chat.qwen.ai — Alibaba's Tongyi Qianwen) selectors.

Qwen's app uses a React composer. Two things it exposes are stable enough to build
on, and the rest is deliberately loose:

  * `#chat-input` — the composer textarea keeps its id across releases.
  * `.markdown-content-container` — the rendered answer body.

Unlike DeepSeek, Qwen DOES have a model dropdown (Qwen3-Max, Qwen3-Plus,
Qwen3-Coder, …) as well as composer switches (Thinking, Web Search, Deep
Research). Both are driven by their visible TEXT rather than by markup — see
src/providers/model_picker.py — and assistant turns are additionally filtered
NEGATIVELY (anything inside a user turn is not an answer).

Thread URL: https://chat.qwen.ai/c/<uuid>
"""

from __future__ import annotations


class QwenSelectors:
    # ── Composer ────────────────────────────────────────────────
    CHAT_INPUT = [
        "textarea#chat-input",
        "#chat-input",
        "textarea[placeholder*='ask' i]",
        "textarea",
        "div[contenteditable='true']",
    ]

    SEND_BUTTON = [
        ".chat-prompt-send-button button.send-button",
        "#send-message-button",
        "button#send-message-button",
        "button[type='submit']",
        "button[aria-label*='send' i]",
        "div[role='button'][aria-label*='send' i]",
    ]

    STOP_BUTTON = [
        ".chat-prompt-send-button button.stop-button",
        "button[aria-label*='stop' i]",
        "#pause-response-button",
    ]

    # ── Conversation turns ──────────────────────────────────────
    ASSISTANT_TURN = [
        "div.markdown-content-container",
        "[class*='markdown-content-container']",
        "[id^='response-content-container']",
        "[class*='response-message']",
    ]
    USER_TURN = "[class*='user-message'], [id^='user-message'], [class*='question-item']"

    # ── Login state ─────────────────────────────────────────────
    # The current app allows guest chat. Require an account menu/avatar and
    # no visible sign-in affordance; a composer alone is not a saved login.
    LOGIN_INDICATORS = [
        "a[href^='/auth']",
        "button:has-text('Sign up')",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
        "a:has-text('Log in')",
        "input[placeholder*='phone' i]",
    ]
    LOGGED_IN_INDICATORS = [
        ".sidebar-user button.user-menu-btn",
        ".sidebar-user button.user-menu-btn-mobile",
        "[class*='user-avatar']",
        "button[aria-label*='account' i]",
    ]

    # ── Attachments ─────────────────────────────────────────────
    FILE_INPUT = ["input[type='file']"]
    UPLOAD_BUTTON = [
        "input[type='file']",
        "button[aria-label*='upload' i]",
        "button[aria-label*='attach' i]",
    ]

    # ── Model picker + modes ────────────────────────────────────
    MODEL_MENU_BUTTON = [
        "button#model-selector-button",
        "[data-testid='model-selector']",
        "button[aria-label*='model' i]",
        "button[aria-haspopup='listbox']",
        "button[aria-haspopup='menu']",
    ]
    MODEL_MENU_ITEM = [
        "[role='option']",
        "[role='menuitem']",
        "[role='menuitemradio']",
        "[class*='model-item']",
        "[class*='model-list'] li",
    ]
    # Thinking / Web Search / Deep Research switches under the composer.
    # Selector ORDER is priority (see src/providers/model_picker.py): the
    # composer's feature row comes first so a page-wide "Search" control can
    # never be mistaken for the composer's Web Search switch.
    MODE_BUTTON = [
        "button[aria-label*='think' i]",
        "[class*='chat-input-feature'] button",
        "[class*='feature-btn']",
        "form button",
        "button[aria-label*='search' i]",
        "button",
    ]
    MEDIA_TOOL = [
        # Qwen web 0.2.91: custom dropdown rows and a removable current mode.
        ".mode-select-current-mode", ".mode-select-common-item",
        ".mode-selector-drawer-list-item",
        "[class*='chat-input-feature'] button", "[class*='feature-btn']",
        "[role='menuitem']", "[role='menuitemradio']", "[role='option']",
        "[class*='tool-item']", "[class*='tools-item']",
        "[class*='chip']", "[class*='tag']", "button", "[role='button']",
    ]
    MODE_MENU_BUTTON = [
        ".mode-select-open[role='button']",
        "[role='button'][aria-label='Select Mode']",
        "button[aria-label*='tools' i]", "[role='button'][aria-label*='tools' i]",
        "button[title*='tools' i]", "button[aria-label='Add']",
        "button[aria-label='More']", "button[aria-label='+']",
        "[class*='chat-input'] button[aria-label*='more' i]",
        "button[aria-label*='more' i]",
        "[class*='chat-input-feature'] button[aria-haspopup]",
    ]
    MEDIA_MODE_CLOSE = [".mode-select-current-mode-close"]

    # ── Media generation (Image Generation / Image Edit / Video) ─
    # Generated media renders inside the assistant's message, but frequently
    # OUTSIDE its markdown body (an image card or player below the text), so the
    # search scope is the whole response message, loosening down to the page.
    # Completion is detected by DIFFING against a baseline taken just before
    # sending, so a looser scope cannot pick up anything that already existed.
    MEDIA_SCOPE = [
        "[class*='response-message']",
        "[id^='response-message']",
        "[class*='chat-response']",
        "[class*='message-assistant']",
        "[class*='markdown-content-container']",
        "main",
        "body",
    ]
    # Never a generated result: the user's own uploads (they reappear in the
    # user turn after sending) and the composer's attachment preview.
    MEDIA_EXCLUDE = (
        "[class*='user-message'], [id^='user-message'], [class*='question-item'], "
        "#chat-input, [class*='chat-input'], form, nav, aside, header"
    )
    # Real results are large; avatars, icons and model logos are not.
    MIN_MEDIA_DIM = 256

    # The video mode's aspect-ratio control. Its trigger usually shows the
    # current ratio ("16:9"), which the shared picker reads to skip a no-op.
    ASPECT_MENU_BUTTON = [
        "button[aria-label*='ratio' i]",
        "[class*='ratio'] button",
        "[class*='aspect'] button",
        "button[aria-haspopup='listbox']",
    ]
    ASPECT_MENU_ITEM = [
        "[role='option']",
        "[role='menuitem']",
        "[role='menuitemradio']",
        "[class*='ratio'] li",
        "[class*='aspect'] li",
    ]

    # Phrases that mean the generation itself failed (limits are caught by the
    # shared limit detector). Kept narrow: a false positive fails a request
    # that might still have produced media.
    MEDIA_FAILURE_TEXT_RE = (
        r"generation failed|failed to generate|unable to generate|"
        r"could not generate|cannot generate|"
        r"violat\w* (?:our|the) (?:content|usage) polic|"
        r"content (?:policy|safety) (?:violation|check)|sensitive content"
    )

    # ── Sidebar ─────────────────────────────────────────────────
    SIDEBAR_THREAD_LINKS = [
        "a[href^='/c/']",
    ]

    NEW_CHAT_BUTTON = [
        "#new-chat-button",
        "a[href='/']",
        "button[aria-label*='new chat' i]",
    ]
