"""
Grok (grok.com) selectors.

Read off the live grok.com DOM:
  input   textarea[aria-label="Ask Grok anything"]  (placeholder "What do you want to know?")
  send    button[aria-label="Submit"]
  user    [data-testid="user-message"]
  bubbles .message-bubble

NOTE ON ASSISTANT TURNS: grok.com puts anonymous visitors behind a sign-up wall
(data-testid="anon-paywall-sign-up-card" appears instead of a reply), so the
assistant-side markup could not be observed without an account. We therefore
identify assistant turns NEGATIVELY — message bubbles that are not inside a
user-message — which holds regardless of what testid Grok gives them. If a
future inspection with a signed-in account reveals a stable assistant testid,
add it to ASSISTANT_TURN and it will be preferred.
"""

from __future__ import annotations


class GrokSelectors:
    # ── Composer ────────────────────────────────────────────────
    # Grok's composer is a contenteditable div (data-testid="chat-input",
    # aria-label "Ask Grok anything"), NOT a textarea. Verified on the live DOM.
    # JS-focus + keyboard.type works on it (same approach as Gemini).
    CHAT_INPUT = [
        "[data-testid='chat-input']",
        "[aria-label='Ask Grok anything']",
        "div[contenteditable='true']",
        "textarea[aria-label='Ask Grok anything']",
        "textarea",
    ]

    SEND_BUTTON = [
        "button[data-testid='send-button']",
        "button[aria-label='Submit']",
        "button[type='submit']",
    ]

    STOP_BUTTON = [
        "button[aria-label*='Stop' i]",
    ]

    # ── Conversation turns ──────────────────────────────────────
    USER_TURN = "[data-testid='user-message']"
    # Preferred positive markers if/when they exist; the negative rule below is
    # the fallback that always works.
    ASSISTANT_TURN = [
        "[data-testid='assistant-message']",
        "[data-testid='model-response']",
    ]
    MESSAGE_BUBBLE = ".message-bubble"

    # ── Login state ─────────────────────────────────────────────
    # Grok gates replies behind auth, so an anonymous session shows a sign-up
    # wall. Either signal means NOT signed in.
    LOGIN_INDICATORS = [
        "[data-testid='anon-paywall-sign-up-card']",
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
    ]
    LOGGED_IN_INDICATORS = [
        "button[aria-label*='Account' i]",
        "[data-testid='user-avatar']",
        "img[alt*='avatar' i]",
    ]

    # ── Attachments / images ────────────────────────────────────
    FILE_INPUT = ["input[type='file']"]
    UPLOAD_BUTTON = [
        "button[aria-label*='Attach' i]",
        "button[aria-label*='Add' i]",
    ]
    # Grok renders generated images ("Imagine") inline in the response.
    GENERATED_IMAGE = [
        ".message-bubble img",
        "img[src*='assets.grok.com']",
        "img[src^='https://']",
    ]

    NEW_CHAT_BUTTON = [
        "a[href='/chat']",
        "button[aria-label*='New' i]",
    ]

    # ── Model picker + modes (text requests) ────────────────────
    # grok.com puts the model choice in a composer dropdown ("Auto" / "Fast" /
    # "Expert" / "Heavy" / "Grok 4.1" ...) and the reasoning/search choices in
    # toggle buttons next to it. Entries are matched by their visible TEXT (see
    # src/providers/model_picker.py) so a renamed tier still resolves; these
    # selectors only have to be broad enough to CONTAIN the entries.
    MODEL_MENU_BUTTON = [
        "[data-testid='model-selector']",
        "button[data-testid='model-selector']",
        "button[aria-label*='model' i]",
        "button[id*='model' i]",
        "form button[aria-haspopup='menu']",
        "button[aria-haspopup='listbox']",
    ]
    MODEL_MENU_ITEM = [
        "[role='menuitemradio']",
        "[role='menuitem']",
        "[role='option']",
        "[data-testid*='model-item']",
        "[data-radix-popper-content-wrapper] button",
    ]
    # Think / DeepSearch / DeeperSearch toggles.
    MODE_BUTTON = [
        "[data-testid='think-button']",
        "[data-testid='deepsearch-button']",
        "button[aria-label*='think' i]",
        "button[aria-label*='search' i]",
        "form button",
        "main button",
    ]
    MODE_MENU_BUTTON = [
        "button[aria-label*='more' i]",
        "form button[aria-haspopup='menu']",
    ]
