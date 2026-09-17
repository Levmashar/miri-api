"""
Provider abstraction — everything that differs between ChatGPT / Gemini / Claude.

Providers run SIMULTANEOUSLY: the provider is a property of an ACCOUNT, not a
global switch. An account's provider decides which client class drives its tabs,
which URL they sit on, how "are we logged in?" is answered, and which model ids
it serves. The pool then guarantees a request only ever lands on a tab of the
provider it asked for.

To add a provider: implement a client with the same small interface the
AccountManager uses, then register a ProviderSpec below.

Client interface (what the rest of the app calls):
    client = SpecClient(page)
    await client.send_message(text, image_paths=..., file_paths=..., expect_image=...) -> ChatResponse
    await client.new_chat()
    await client.navigate_to_thread(thread_id)
    client._extract_thread_id() -> str
    await client.list_threads() -> list[dict]     (optional; may return [])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# Chat-model registries. These modules are pure data + string helpers (no
# patchright), so importing them here keeps the "importing base never pulls in a
# browser" rule intact.
from src.providers.deepseek import models as deepseek_models
from src.providers.chatgpt import chat_models as chatgpt_models
from src.providers.gemini import models as gemini_models
from src.providers.grok import models as grok_models
from src.providers.qwen import models as qwen_models
from src.providers.qwen.selectors import QwenSelectors

GEMINI_CHAT_MODELS = gemini_models.CHAT_MODEL_IDS
GROK_CHAT_MODELS = grok_models.CHAT_MODEL_IDS
DEEPSEEK_CHAT_MODELS = deepseek_models.CHAT_MODEL_IDS
QWEN_CHAT_MODELS = qwen_models.CHAT_MODEL_IDS
QWEN_IMAGE_MODELS = qwen_models.IMAGE_MODEL_IDS
QWEN_VIDEO_MODELS = qwen_models.VIDEO_MODEL_IDS

CHATGPT = "chatgpt"
GEMINI = "gemini"
CLAUDE = "claude"
GROK = "grok"
SEEDREAM = "seedream"
DEEPSEEK = "deepseek"
QWEN = "qwen"


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one provider."""
    id: str
    label: str
    url: str                       # where a fresh tab starts
    chat_model_id: str             # model id reported for chat ("" if no chat)
    image_model_id: str            # default/primary image model id ("" if none)
    supports_images: bool          # can it generate images at all?
    # Login detection: a VISIBLE match on any login_indicator means NOT signed in
    # (checked first — some providers allow anonymous use, so the presence of a
    # chat box proves nothing). logged_in_indicators are positive markers.
    chat_input: tuple
    login_indicators: tuple
    logged_in_indicators: tuple
    # Domains to pre-resolve for Chrome DNS (see core dns pinning)
    domains: tuple
    # Lazy client factory so importing this module never pulls in patchright.
    client_factory: Callable = field(repr=False, default=None)
    # All selectable image models (regular, pro, ...); first is the default.
    # For Gemini: ("nano-banana", "nano-banana-pro"). Empty -> just image_model_id.
    image_models: tuple = ()
    # All CHAT model ids this provider serves, i.e. every model its web UI lets
    # you pick for a text request. The first entry is the default ("don't touch
    # the picker"), and is always chat_model_id. Empty -> just chat_model_id.
    # The id -> UI-label mapping lives in src/providers/<p>/models.py.
    chat_models: tuple = ()
    # ── Capability flags (default to the chat-provider shape) ──
    # Whether this provider serves /v1/chat/completions & /v1/responses. False for
    # media-only providers (Seedream is video-only) so those endpoints 501 cleanly
    # instead of driving a video UI with a chat prompt.
    supports_chat: bool = True
    # Video generation (Seedream/Dreamina). supports_video gates /v1/videos/*.
    supports_video: bool = False
    video_model_id: str = ""       # default/primary video model id
    # All selectable video models; first is the default. The id->UI-label +
    # capability matrix lives in src/providers/seedream/models.py.
    video_models: tuple = ()
    # Which /v1/videos modes this provider can serve (text_to_video,
    # image_to_video, first_last_frame, ...). Empty = every mode (Seedream).
    # The endpoint rejects any other mode with a 400 before a tab is taken.
    video_modes: tuple = ()
    # Providers with an anonymous composer need an account marker as well.
    login_requires_account_marker: bool = False

    def make_client(self, page):
        return self.client_factory(page)


def _chatgpt_client(page):
    from src.providers.chatgpt.client import ChatGPTClient
    return ChatGPTClient(page)


def _gemini_client(page):
    from src.providers.gemini.client import GeminiClient
    return GeminiClient(page)


def _grok_client(page):
    from src.providers.grok.client import GrokClient
    return GrokClient(page)


def _claude_client(page):
    from src.providers.claude.client import ClaudeClient
    return ClaudeClient(page)


def _deepseek_client(page):
    from src.providers.deepseek.client import DeepSeekClient
    return DeepSeekClient(page)


def _qwen_client(page):
    from src.providers.qwen.client import QwenClient
    return QwenClient(page)


def _seedream_client(page):
    from src.providers.seedream.client import SeedreamClient
    return SeedreamClient(page)


_CHATGPT = ProviderSpec(
    id=CHATGPT,
    label="ChatGPT",
    url="https://chatgpt.com",
    chat_model_id="chatgpt-browser",
    chat_models=chatgpt_models.CHAT_MODEL_IDS,
    image_model_id="chatgpt-image",
    supports_images=True,
    chat_input=(
        "#prompt-textarea",
        "div[contenteditable='true']",
        "textarea[data-id='root']",
    ),
    login_indicators=(
        "button[data-testid='login-button']",
        "button[data-testid='signup-button']",
        "[data-testid='login-button']",
        "[data-testid='signup-button']",
        "button:has-text('Log in')",
        "button:has-text('Sign up')",
    ),
    logged_in_indicators=(),
    domains=(
        "chatgpt.com", "cdn.oaistatic.com", "ab.chatgpt.com", "ws.chatgpt.com",
        "auth.openai.com", "auth0.openai.com", "openai.com", "api.openai.com",
        "platform.openai.com", "tcr9i.chat.openai.com",
        "files.oaiusercontent.com", "persistent.oaistatic.com",
    ),
    client_factory=_chatgpt_client,
)

_GEMINI = ProviderSpec(
    id=GEMINI,
    label="Gemini",
    login_requires_account_marker=True,
    url="https://gemini.google.com/app",
    chat_model_id="gemini-browser",
    # Every model the Gemini app's picker offers for a TEXT request
    # (gemini-fast / -thinking / -pro / ...), plus the Deep Research mode. See
    # src/providers/gemini/models.py for the id -> UI-label mapping.
    chat_models=GEMINI_CHAT_MODELS,
    # The Gemini app generates with Nano Banana 2 (regular); Nano Banana Pro is a
    # paid "Redo with Nano Banana Pro" upgrade step. Callers pick per request:
    #   nano-banana      -> regular (no upgrade)
    #   nano-banana-pro  -> run the upgrade (paid plans only)
    image_model_id="nano-banana",
    image_models=("nano-banana", "nano-banana-pro"),
    supports_images=True,
    chat_input=(
        "div.ql-editor[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "rich-textarea div[contenteditable='true']",
    ),
    login_indicators=(
        "a[aria-label*='Sign in']",
        "a:has-text('Sign in')",
        "button:has-text('Sign in')",
    ),
    # Gemini permits ANONYMOUS chatting, so a chat box proves nothing. The
    # account avatar/menu is the positive proof of a signed-in session.
    logged_in_indicators=(
        "a[aria-label*='Google Account']",
        "img[alt*='Profile']",
        "[data-test-id='user-avatar']",
    ),
    domains=(
        "gemini.google.com", "accounts.google.com", "google.com",
        "www.google.com", "apis.google.com", "ssl.gstatic.com",
        "www.gstatic.com", "lh3.googleusercontent.com",
        "play.google.com", "content-push.googleapis.com",
    ),
    client_factory=_gemini_client,
)

_GROK = ProviderSpec(
    id=GROK,
    label="Grok",
    url="https://grok.com",
    chat_model_id="grok-browser",
    # Every model/mode grok.com offers for a TEXT request (grok-fast / -expert /
    # -heavy / -think / -deepsearch / ...). See src/providers/grok/models.py.
    chat_models=GROK_CHAT_MODELS,
    image_model_id="grok-imagine",
    supports_images=True,
    # Grok migrated its composer from a <textarea> to a contenteditable div
    # (data-testid="chat-input", aria-label "Ask Grok anything"). Verified against
    # the live logged-in DOM.
    chat_input=(
        "[data-testid='chat-input']",
        "[aria-label='Ask Grok anything']",
        "div[contenteditable='true']",
        "textarea[aria-label='Ask Grok anything']",
        "textarea",
    ),
    # grok.com gates replies behind a sign-up wall for anonymous visitors, so
    # the paywall card is itself a decisive "not signed in" signal.
    login_indicators=(
        "[data-testid='anon-paywall-sign-up-card']",
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
    ),
    # Positive proof of a signed-in session (verified on the live DOM): the
    # sidebar "New Chat" control and the composer only render once logged in
    # (anonymous visitors get the paywall card caught above).
    logged_in_indicators=(
        "[data-testid='new-chat']",
        "[data-testid='chat-input']",
        "button[aria-label*='Account' i]",
        "[data-testid='user-avatar']",
    ),
    domains=(
        "grok.com", "assets.grok.com", "x.ai", "accounts.x.ai",
        "api.x.com", "x.com", "abs.twimg.com",
    ),
    client_factory=_grok_client,
)

_CLAUDE = ProviderSpec(
    id=CLAUDE,
    label="Claude",
    url="https://claude.ai",
    chat_model_id="claude-browser",
    image_model_id="",
    supports_images=False,
    chat_input=("div[contenteditable='true']",),
    login_indicators=("button:has-text('Log in')", "a:has-text('Log in')"),
    logged_in_indicators=("button[data-testid='user-menu-button']",),
    domains=("claude.ai", "api.claude.ai", "cdn.claude.ai", "anthropic.com", "www.anthropic.com"),
    client_factory=_claude_client,
)

# DeepSeek — chat.deepseek.com. Text only: no image or video surface. Its model
# choice is two composer switches (DeepThink, Search) rather than a dropdown;
# see src/providers/deepseek/models.py.
_DEEPSEEK = ProviderSpec(
    id=DEEPSEEK,
    label="DeepSeek",
    url="https://chat.deepseek.com",
    chat_model_id="deepseek-browser",
    chat_models=DEEPSEEK_CHAT_MODELS,
    image_model_id="",
    supports_images=False,
    chat_input=(
        "textarea#chat-input",
        "#chat-input",
        "textarea",
    ),
    # DeepSeek requires an account to chat at all, so a visible sign-in
    # affordance is the decisive "not signed in" signal.
    login_indicators=(
        "button:has-text('Log in')",
        "a:has-text('Log in')",
        "button:has-text('Sign in')",
    ),
    # The composer only renders for a signed-in session.
    logged_in_indicators=(
        "textarea#chat-input",
        "[class*='ds-avatar']",
    ),
    domains=(
        "chat.deepseek.com", "deepseek.com", "www.deepseek.com",
        "cdn.deepseek.com", "api.deepseek.com",
    ),
    client_factory=_deepseek_client,
)

# Qwen — Alibaba's chat.qwen.ai (Tongyi Qianwen). Chat, IMAGE and VIDEO: the
# app generates media from the same composer switched into "Image Generation",
# "Image Edit" or "Video Generation" mode. Chat model choice is a dropdown PLUS
# composer switches; media is the mode switch. See src/providers/qwen/models.py
# (ids + labels) and the media pipeline in src/providers/qwen/client.py.
_QWEN = ProviderSpec(
    id=QWEN,
    label="Qwen",
    login_requires_account_marker=True,
    url="https://chat.qwen.ai",
    chat_model_id="qwen-browser",
    chat_models=QWEN_CHAT_MODELS,
    # qwen-image (Image Generation) / qwen-image-edit (Image Edit — chosen
    # automatically whenever reference images are attached).
    image_model_id="qwen-image",
    image_models=QWEN_IMAGE_MODELS,
    supports_images=True,
    # Wan video: text -> video, or ONE start image -> video. No first/last
    # frame, keyframe or multi-reference inputs (those are Seedream's).
    supports_video=True,
    video_model_id="qwen-video",
    video_models=QWEN_VIDEO_MODELS,
    video_modes=qwen_models.VIDEO_MODES,
    chat_input=tuple(QwenSelectors.CHAT_INPUT),
    login_indicators=tuple(QwenSelectors.LOGIN_INDICATORS),
    logged_in_indicators=tuple(QwenSelectors.LOGGED_IN_INDICATORS),
    domains=(
        "chat.qwen.ai", "qwen.ai", "www.qwen.ai",
        "tongyi.aliyun.com", "aliyun.com", "alicdn.com",
        "g.alicdn.com", "sso.alibaba.com",
        # Where generated media is served from, so results render (and
        # download) without Chrome's in-container DNS. Region-specific OSS
        # result buckets can't be pinned by wildcard; a proxy — recommended
        # anyway — skips DNS pinning entirely.
        "cdn.qwenlm.ai", "img.alicdn.com", "assets.alicdn.com",
    ),
    client_factory=_qwen_client,
)

# Seedream — ByteDance's Dreamina (dreamina.capcut.com), VIDEO generation only.
# Selectors/flow mirror a public Playwright driver of the same UI
# (github.com/MikeChongCan/10x-chat) and are re-verified in the seedream package;
# see src/providers/seedream/selectors.py. The model ids below are the API names
# a caller passes as `model`; the id -> UI-dropdown-label + capability matrix
# lives in src/providers/seedream/models.py.
SEEDREAM_VIDEO_MODELS = (
    "seedance-2.5",
    "seedance-2.0",
    "seedance-2.0-fast",
    "seedance-2.0-mini",
    "seedance-1.5-pro",
    "seedance-1.0-pro",
    "seedance-1.0-lite",
    "video-3.0",
    "video-3.0-pro",
    "video-s2.0-pro",
    "video-1.0",
)

# Dreamina IMAGE models. In the model dropdown the ByteDance models are labelled
# "Image X.Y" (subtitle "by Seedream X.Y"); the slug -> UI-label mapping lives in
# src/providers/seedream/models.py. Only the Seedream-branded models are exposed
# here — Dreamina also pipes in Nano Banana / GPT Image, but those ids already
# belong to the gemini/chatgpt providers, so exposing them here would make model
# -> provider routing ambiguous.
SEEDREAM_IMAGE_MODELS = (
    "seedream-5.0-pro",
    "seedream-5.0-lite",
    "seedream-4.5",
    "seedream-4.0",
    "seedream-3.0",
)

_SEEDREAM = ProviderSpec(
    id=SEEDREAM,
    label="Seedream",
    url="https://dreamina.capcut.com/ai-tool/home?type=video",
    chat_model_id="",          # no chat surface
    image_model_id="seedream-4.5",
    image_models=SEEDREAM_IMAGE_MODELS,
    supports_images=True,
    supports_chat=False,
    supports_video=True,
    video_model_id="seedance-2.0",
    video_models=SEEDREAM_VIDEO_MODELS,
    # Only used for login detection (the composer is present once signed in and
    # on the generate view). Dreamina requires login to use the tool at all.
    chat_input=(
        ".tiptap.ProseMirror[role='textbox']",
        ".tiptap.ProseMirror",
        "div[contenteditable='true']",
    ),
    # A visible login button (Dreamina redirects anonymous visitors to sign in)
    # is the decisive "not signed in" signal.
    login_indicators=(
        "[class*='login-button']",
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
        "button:has-text('Log in')",
    ),
    # The sidebar credit balance only renders for a signed-in account.
    logged_in_indicators=(
        "#SiderMenuCredit",
        "[class*='credit']",
    ),
    domains=(
        "dreamina.capcut.com", "capcut.com", "www.capcut.com",
        "sso.capcut.com", "commonweb.capcut.com",
        "dreamina-api.us.capcut.com", "mweb-api-sg.capcut.com",
        "sf16-web-login-neutral.capcutstatic.com", "capcutstatic.com",
        # Login is federated to the ByteDance/TikTok/Google account systems.
        "accounts.google.com", "www.tiktok.com", "byteoversea.com",
    ),
    client_factory=_seedream_client,
)

PROVIDERS: dict[str, ProviderSpec] = {
    CHATGPT: _CHATGPT,
    GEMINI: _GEMINI,
    GROK: _GROK,
    CLAUDE: _CLAUDE,
    DEEPSEEK: _DEEPSEEK,
    QWEN: _QWEN,
    SEEDREAM: _SEEDREAM,
}

# Providers offered in the UI / usable for new accounts.
ENABLED_PROVIDER_IDS = (CHATGPT, GEMINI, GROK, CLAUDE, DEEPSEEK, QWEN, SEEDREAM)


def get_provider(provider_id: str) -> ProviderSpec:
    spec = PROVIDERS.get((provider_id or "").lower())
    if spec is None:
        raise ValueError(
            f"unknown provider {provider_id!r} (known: {', '.join(PROVIDERS)})"
        )
    return spec


# Providers whose web UI exposes a CHOICE of chat models. The module maps an
# API model id -> the UI label to click; providers absent here serve exactly one
# chat model and ignore the `chat_model` argument (currently Claude).
_CHAT_MODEL_MODULES = {
    CHATGPT: chatgpt_models,
    GEMINI: gemini_models,
    GROK: grok_models,
    DEEPSEEK: deepseek_models,
    QWEN: qwen_models,
}


def resolve_chat_model(provider_id: str, requested: str) -> str:
    """Canonical chat-model id for `requested`, or "" to leave the UI alone.

    "" is returned for a single-model provider, for an unknown id, and for the
    provider's default id — all of which mean "answer on whatever model the
    account currently has selected". An unknown model id must never silently
    switch an account to a different model.
    """
    mod = _CHAT_MODEL_MODULES.get((provider_id or "").lower())
    if mod is None:
        return ""
    resolved = mod.resolve_chat_model_id(requested or "")
    return "" if resolved == mod.DEFAULT_MODEL_ID else resolved


def chat_model_catalog(provider_id: str) -> list[dict]:
    """[{id, description, long}] for every chat model a provider serves.

    Single-model providers report their one id with an empty description.
    """
    spec = get_provider(provider_id)
    if not spec.supports_chat:
        return []
    mod = _CHAT_MODEL_MODULES.get(spec.id)
    if mod is None:
        return [{"id": spec.chat_model_id, "description": "", "long": False}] if spec.chat_model_id else []
    return [
        {"id": mid, "description": mod.describe(mid), "long": mod.is_long(mid)}
        for mid in mod.CHAT_MODEL_IDS
    ]


def all_domains() -> list[str]:
    """Every provider's domains, for the Chrome DNS pin + /etc/hosts seeding."""
    seen: list[str] = []
    for spec in PROVIDERS.values():
        for d in spec.domains:
            if d not in seen:
                seen.append(d)
    # Shared infrastructure used by the provider front-ends.
    for d in ("challenges.cloudflare.com", "static.cloudflareinsights.com"):
        if d not in seen:
            seen.append(d)
    return seen


def model_id_to_provider(model: str) -> str | None:
    """Map a requested model id to a provider id, or None if unrecognised."""
    m = (model or "").strip().lower()
    if not m:
        return None
    for spec in PROVIDERS.values():
        if spec.chat_model_id and m == spec.chat_model_id:
            return spec.id
        if spec.chat_models and m in spec.chat_models:
            return spec.id
        if spec.image_model_id and m == spec.image_model_id:
            return spec.id
        if spec.image_models and m in spec.image_models:
            return spec.id
        if spec.video_models and m in spec.video_models:
            return spec.id
    # Friendly aliases / substring routing so callers can say "gemini" or "gpt".
    if "gemini" in m or "nano-banana" in m or "nano_banana" in m:
        return GEMINI
    if "grok" in m:
        return GROK
    if "claude" in m:
        return CLAUDE
    if "seedance" in m or "seedream" in m or "dreamina" in m:
        return SEEDREAM
    if "deepseek" in m:
        return DEEPSEEK
    if "qwen" in m or "tongyi" in m:
        return QWEN
    if m == "wan" or m.startswith(("wan-", "wanx")):
        return QWEN            # Alibaba's Wan video model, served via Qwen
    if "gpt" in m or "chatgpt" in m or "dall-e" in m:
        return CHATGPT
    return None
