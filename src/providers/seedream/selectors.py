"""
Seedream / Dreamina (dreamina.capcut.com) selectors.

These mirror a public, working Playwright driver of the SAME web UI
(github.com/MikeChongCan/10x-chat — files src/providers/dreamina-video.ts and
src/core/dreamina-video-orchestrator.ts), cross-checked against Dreamina's docs.
They are therefore grounded in real source, but the account was not yet logged
in when this was written, so treat them as VERIFIED-FROM-A-THIRD-PARTY-DRIVER,
to be re-confirmed against the live DOM once signed in.

The UI is ByteDance's "Lv" (Arco-derived) design system: controls carry `lv-`
classes and the prompt box is a TipTap/ProseMirror editor.
"""

from __future__ import annotations


class SeedreamSelectors:
    # ── Views ───────────────────────────────────────────────────
    HOME_URL = "https://dreamina.capcut.com/ai-tool/home?type=video"
    IMAGE_HOME_URL = "https://dreamina.capcut.com/ai-tool/home?type=image"
    # After clicking generate the app navigates here.
    GENERATE_URL_GLOB = "**/ai-tool/generate**"

    # ── Composer (TipTap/ProseMirror) ───────────────────────────
    # Disambiguate the PROMPT box from the references box by preferring the one
    # whose data-placeholder/aria-placeholder is not the references hint.
    COMPOSER = (
        ".tiptap.ProseMirror[role='textbox']",
        ".tiptap.ProseMirror",
        "div[contenteditable='true'][role='textbox']",
    )

    # ── Dropdowns (model, reference-mode, duration) ─────────────
    SELECT_TRIGGER = ".lv-select[role='combobox']"
    SELECT_POPUP = ".lv-select-popup"
    SELECT_OPTION = ".lv-select-option"

    # ── Aspect ratio / resolution toggles ───────────────────────
    # `button.lv-btn` whose visible text is an aspect ("16:9") or resolution
    # ("1080P"). Matched by text in JS; no single stable id.
    LV_BUTTON = "button.lv-btn"

    # ── Uploads (omni refs / first+last frames / keyframes) ─────
    FILE_INPUT = ("input[type='file']",)

    # ── Generate / submit ───────────────────────────────────────
    SUBMIT = (
        "button[class*='submit-button']:not(.lv-btn-disabled)",
        "button.lv-btn-primary.lv-btn-shape-circle:not(.lv-btn-disabled)",
    )

    # ── Result ──────────────────────────────────────────────────
    RESULT_VIDEO = "[class*='responsive-video-grid'] video"
    # The image result grid mirrors the video one's naming (INFERRED — the video
    # selector is verified from a public driver; re-confirm live). Broad fallbacks
    # catch result-card <img> regardless of the exact grid class; a dimension +
    # not-placeholder filter (see detector) does the real work.
    RESULT_IMAGE = (
        "[class*='responsive-image-grid'] img, "
        "[class*='image-grid'] img, "
        "[class*='record'] img, [class*='result'] img, [class*='generate'] img"
    )

    # ── Login state ─────────────────────────────────────────────
    # Dreamina forces login to use the tool at all; an anonymous visit shows a
    # login button and an empty credit balance.
    LOGIN_INDICATORS = (
        "[class*='login-button']",
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
        "button:has-text('Log in')",
    )
    LOGGED_IN_INDICATORS = (
        "#SiderMenuCredit",
        "[class*='credit']",
    )


# ── Completion / failure heuristics (from the 10x-chat driver) ──
# The loading placeholder is a spinner mp4 served from capcutstatic.com; the
# finished result is either a blob: URL or a CDN mp4 whose path contains /video/.
# Key on these heuristics, NEVER on a hardcoded CDN host (production hosts vary:
# v*.capcut.com, *.byteimg.com, ...).
SPINNER_SRC_RE = r"capcutstatic\.com"
ACCEPT_VIDEO_PATH_RE = r"/video/"
# A finished image: a loaded <img> (naturalWidth over this) that isn't a
# capcutstatic placeholder or a data: URI. Dreamina serves 4 variants per
# text→image request from a signed ByteDance ImageX CDN (*.byteimg.com, tplv/tos
# paths); we key on load-state + dimensions, NOT a hardcoded host.
MIN_IMAGE_DIM = 128
EXPECTED_IMAGES = 4

# Error / credit text is read ONLY from transient alert surfaces (toasts,
# notifications, dialogs, the result-card error node) — never the whole page.
# Scanning document.body.innerText would match persistent nav/credit/upgrade
# chrome ("Upgrade to Pro", "Buy credits", the sidebar balance) on the very first
# poll and wrongly take the account offline. These are the ByteDance "Lv"
# (Arco) feedback components plus generic ARIA/toast fallbacks.
ERROR_SURFACE = (
    ".lv-message, .lv-notification, .lv-notification-content, .lv-message-content, "
    ".lv-modal, .lv-modal-content, .lv-toast, .lv-popover-content, "
    "[role='alert'], [role='alertdialog'], [class*='toast'], [class*='error-tip'], "
    "[class*='responsive-video-grid'] [class*='error'], [class*='responsive-video-grid'] [class*='fail']"
)

FAILURE_TEXT_RE = r"generation failed|failed to generate|something went wrong|content.*violat"
# Credit exhaustion / paywall — treated as a usage-limit so the account cools
# down. ONLY unambiguous "you are out of credits" phrasings; deliberately NOT
# generic CTAs (upgrade / buy credits / top up / daily limit) which render as
# persistent chrome and would false-positive on a perfectly healthy page.
CREDIT_LIMIT_TEXT_RE = (
    r"insufficient credits?|not enough credits?|run out of credits?|"
    r"out of credits?|no credits? (?:left|remaining)|credits? (?:have )?run out|"
    r"credit balance is (?:0|zero|insufficient)"
)
