"""
Gemini web-app selectors.

Every selector here was read off the live gemini.google.com/app DOM rather than
guessed. Gemini is an Angular app (formerly Bard) built from custom elements —
`user-query`, `model-response`, `message-content` — and the composer is a Quill
editor, not a textarea.

Verified live:
  input        div.ql-editor[contenteditable="true"]  (aria-label "Enter a prompt for Gemini")
  send         button[aria-label="Send message"]
  user turn    <user-query>
  model turn   <model-response>
  answer text  <message-content>  (innerText is the reply)
  thread url   https://gemini.google.com/app/<hex id>
"""

from __future__ import annotations


class GeminiSelectors:
    # ── Composer ────────────────────────────────────────────────
    CHAT_INPUT = [
        "div.ql-editor[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "rich-textarea div[contenteditable='true']",
    ]

    SEND_BUTTON = [
        "button[aria-label='Send message']",
        "button[aria-label*='Send' i]",
    ]

    # Shown while a reply is streaming; disappearing means generation finished.
    STOP_BUTTON = [
        "button[aria-label='Stop response']",
        "button[aria-label*='Stop' i]",
    ]

    # ── Conversation turns ──────────────────────────────────────
    USER_TURN = "user-query"
    MODEL_TURN = "model-response"
    # The element whose innerText is the assistant's answer.
    MESSAGE_CONTENT = "message-content"

    # ── Login state ─────────────────────────────────────────────
    # Gemini allows ANONYMOUS chatting, so the presence of a composer proves
    # nothing about being signed in. A visible "Sign in" is the decisive negative.
    LOGIN_INDICATORS = [
        "a[aria-label*='Sign in']",
        "a:has-text('Sign in')",
        "button:has-text('Sign in')",
    ]
    LOGGED_IN_INDICATORS = [
        "a[aria-label*='Google Account']",
        "img[alt*='Profile']",
        "[data-test-id='user-avatar']",
    ]

    # ── Attachments ─────────────────────────────────────────────
    FILE_INPUT = ["input[type='file']"]
    UPLOAD_BUTTON = [
        "button[aria-label*='Add files' i]",
        "button[aria-label*='upload' i]",
        "button[aria-label*='Open upload' i]",
    ]

    # ── Generated images ────────────────────────────────────────
    # Gemini renders generated images inside the model response.
    GENERATED_IMAGE = [
        "model-response img[src^='https://']",
        "model-response img",
    ]

    # ── Model picker (text requests) ────────────────────────────
    # The composer's model switcher ("Fast" / "Thinking" / "Pro" / ...). Google
    # has shipped this as a Material menu under several test-ids over the years,
    # so every spelling we have seen is listed; the ENTRIES are then matched by
    # their visible text (see src/providers/model_picker.py), which survives the
    # markup churn that broke earlier selector-only approaches.
    MODEL_MENU_BUTTON = [
        "button[data-test-id='bard-mode-menu-button']",
        "[data-test-id='bard-mode-menu-button']",
        "[data-test-id='model-selector'] button",
        "button[data-test-id='model-selector']",
        "bard-mode-switcher button",
        "button[aria-label*='model' i]",
    ]
    MODEL_MENU_ITEM = [
        "button[role='menuitemradio']",
        "[role='menuitemradio']",
        "[role='menuitem']",
        "[role='option']",
        ".bard-mode-list-button",
        ".mat-mdc-menu-item",
        "mat-option",
    ]

    # ── Composer tools (Deep Research) ──────────────────────────
    # Tools live either as chips under the composer or behind a "Tools"/"+"
    # menu, depending on the release. Both paths are tried, text-matched.
    TOOLS_MENU_BUTTON = [
        "button[aria-label*='tools' i]",
        "toolbox-drawer button[aria-label*='more' i]",
        "toolbox-drawer-item button",
        "button[aria-label*='Open tools' i]",
    ]
    TOOL_BUTTON = [
        "toolbox-drawer-item button",
        "toolbox-drawer button",
        "[role='menuitemcheckbox']",
        "[role='menuitem']",
        "input-container button",
        "form button",
    ]
    # Deep Research answers with a PLAN first and only starts once confirmed.
    START_RESEARCH_BUTTON = [
        "model-response button",
        "deep-research-immersive-panel button",
        "main button",
    ]

    # ── New chat ────────────────────────────────────────────────
    NEW_CHAT_BUTTON = [
        "button[aria-label*='New chat' i]",
        "side-nav-action-button button",
    ]

    # ── Nano Banana Pro upgrade ─────────────────────────────────
    # Since Feb 2026 the Gemini app generates with Nano Banana 2 by default;
    # Nano Banana Pro is reached by REGENERATING an existing image via the
    # result's overflow menu ("Redo with Nano Banana Pro"). That entry only
    # exists on paid Google AI plans (Plus / Pro / Ultra).
    #
    # Matched by TEXT rather than DOM structure — Google reshuffles the markup
    # far more often than it renames a user-visible menu entry.
    IMAGE_OVERFLOW_BUTTON = [
        "model-response button[aria-label*='More options' i]",
        "model-response button[aria-label*='More' i]",
        "model-response button[mattooltip*='More' i]",
        "model-response [data-test-id*='overflow'] button",
    ]
    NANO_BANANA_PRO_MENU_TEXT = "nano banana pro"
