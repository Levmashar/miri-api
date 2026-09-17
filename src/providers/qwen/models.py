"""
Qwen CHAT-model registry — the models and modes chat.qwen.ai offers for a text
request, exposed as OpenAI-style model ids.

Same shape as the Gemini/Grok/DeepSeek registries (see
src/providers/gemini/models.py for the design notes). Qwen splits the choice
across two controls, and both are represented here:

  * a model DROPDOWN         — Qwen3-Max, Qwen3-Plus, Qwen3-Coder, Qwen3-VL, …
  * composer SWITCHES        — Thinking, Web Search, Deep Research

so `qwen3-max-thinking` is one dropdown pick plus one switch, and the switch-only
ids (`qwen-thinking`, `qwen-search`) layer on whatever model is selected.
Alibaba renames dropdown entries between releases and gates several behind an
account tier, so every entry lists aliases and selection degrades gracefully:
an entry that is not offered leaves the account on its current model.

Switches are STICKY per tab, so `qwen-browser` is not merely "do nothing" — the
client also switches OFF anything a previous request turned on. See apply_modes()
in src/providers/model_picker.py.
"""

from __future__ import annotations

# The video endpoint's mode vocabulary is defined once, in the Seedream registry
# (pure data — no browser imports), and shared by every video provider.
from src.providers.seedream.models import MODE_I2V, MODE_TEXT

# The id that means "leave the picker alone, all switches off".
DEFAULT_MODEL_ID = "qwen-browser"

_THINKING = ("Thinking", "Deep Thinking", "Think")
_SEARCH = ("Web Search", "Search", "Web search")
_RESEARCH = ("Deep Research", "Research")

# id -> {labels, tool_groups, aliases, long, desc}
CHAT_MODELS: "dict[str, dict]" = {
    "qwen-browser": {
        "labels": (),
        "tool_groups": (),
        "aliases": ("qwen", "qwen-default", "qwen3"),
        "long": False,
        "desc": "Whatever model the account currently has selected (no switching)",
    },
    "qwen3-max": {
        "labels": ("Qwen3-Max", "Qwen3 Max", "Qwen3-Max-Preview"),
        "tool_groups": (),
        "aliases": ("qwen-max", "qwen3max"),
        "long": False,
        "desc": "The flagship Max tier",
    },
    "qwen3-max-thinking": {
        "labels": ("Qwen3-Max", "Qwen3 Max"),
        "tool_groups": (_THINKING,),
        "aliases": ("qwen-max-thinking", "qwen3-max-think"),
        "long": False,
        "desc": "Max with the Thinking switch on — reasoning, slower",
    },
    "qwen3-plus": {
        "labels": ("Qwen3-Plus", "Qwen3 Plus"),
        "tool_groups": (),
        "aliases": ("qwen-plus",),
        "long": False,
        "desc": "The mid Plus tier",
    },
    "qwen3-flash": {
        "labels": ("Qwen3-Flash", "Qwen3 Flash", "Qwen3-Turbo", "Turbo"),
        "tool_groups": (),
        "aliases": ("qwen-flash", "qwen-turbo", "qwen3-turbo"),
        "long": False,
        "desc": "The fastest tier",
    },
    "qwen3-coder": {
        "labels": ("Qwen3-Coder", "Qwen3 Coder", "Coder"),
        "tool_groups": (),
        "aliases": ("qwen-coder",),
        "long": False,
        "desc": "The code-specialised model",
    },
    "qwen3-vl": {
        "labels": ("Qwen3-VL", "Qwen3 VL", "Qwen3-VL-Plus"),
        "tool_groups": (),
        "aliases": ("qwen-vl", "qwen3-vl-plus"),
        "long": False,
        "desc": "The vision-language model — pair it with image attachments",
    },
    "qwen3-omni": {
        "labels": ("Qwen3-Omni", "Qwen3 Omni", "Qwen3-Omni-Flash"),
        "tool_groups": (),
        "aliases": ("qwen-omni", "qwen3-omni-flash"),
        "long": False,
        "desc": "The omni-modal model, where the account offers it",
    },
    "qwen-thinking": {
        "labels": (),
        "tool_groups": (_THINKING,),
        "aliases": ("qwen-think",),
        "long": False,
        "desc": "Thinking switch on top of the current model",
    },
    "qwen-search": {
        "labels": (),
        "tool_groups": (_SEARCH,),
        "aliases": ("qwen-web", "qwen-websearch"),
        "long": False,
        "desc": "Web search grounding on the current model",
    },
    "qwen-deep-research": {
        "labels": (),
        "tool_groups": (_RESEARCH,),
        "aliases": ("qwen-research",),
        "long": True,
        "desc": "Multi-step web research; answers in minutes, not seconds",
    },
}

CHAT_MODEL_IDS = tuple(CHAT_MODELS)


def _norm(s: str) -> str:
    """Normalise a model id for tolerant matching (dots/dashes/spaces/case)."""
    return (s or "").strip().lower().replace("_", "-").replace(" ", "-").replace(".", "-")


_NORM_INDEX: "dict[str, str]" = {}
for _mid, _meta in CHAT_MODELS.items():
    _NORM_INDEX[_norm(_mid)] = _mid
    for _alias in _meta.get("aliases", ()):
        _NORM_INDEX.setdefault(_norm(_alias), _mid)


def resolve_chat_model_id(requested: str) -> str:
    """Canonical chat-model id for a requested string, or "" if unrecognised."""
    req = _norm(requested)
    if not req:
        return ""
    if req in _NORM_INDEX:
        return _NORM_INDEX[req]
    for mid, meta in CHAT_MODELS.items():
        if any(_norm(lbl) == req for lbl in meta.get("labels", ())):
            return mid
        if any(_norm(t) == req for grp in meta.get("tool_groups", ()) for t in grp):
            return mid
    return ""


def label_candidates(model_id: str) -> list:
    """Model-dropdown labels to try when selecting `model_id`."""
    return list(CHAT_MODELS.get(model_id, {}).get("labels", ()))


def tool_groups(model_id: str) -> list:
    """Composer switches to enable for `model_id`, one label-group per switch."""
    return [list(grp) for grp in CHAT_MODELS.get(model_id, {}).get("tool_groups", ())]


def all_tool_groups() -> list:
    """Every switch this registry can drive, de-duplicated — chat AND media.

    Used once per tab to CLEAR modes the UI kept from an earlier session: the
    browser profile is persistent, so a mode switched on days ago is still on,
    and this client has no memory of it. The media modes (Image Generation,
    Video Generation, ...) are included: a tab left in Image Generation would
    otherwise answer the next chat request with a picture. Clearing is safe
    because set_mode() only clicks a switch OFF when the page positively reports
    it as on.
    """
    seen, out = set(), []
    groups = [grp for meta in CHAT_MODELS.values() for grp in meta.get("tool_groups", ())]
    for grp in groups + list(MEDIA_MODE_GROUPS):
        key = tuple(grp)
        if key not in seen:
            seen.add(key)
            out.append(list(grp))
    return out


def is_long(model_id: str) -> bool:
    """Whether `model_id` needs the long-mode completion deadline."""
    return bool(CHAT_MODELS.get(model_id, {}).get("long"))


def describe(model_id: str) -> str:
    return CHAT_MODELS.get(model_id, {}).get("desc", "")


# ── Media: image + video generation ─────────────────────────────
#
# chat.qwen.ai generates media from the SAME composer as chat, switched into a
# dedicated mode — "Image Generation" (Qwen-Image), "Image Edit"
# (Qwen-Image-Edit) or "Video Generation" (Alibaba's Wan video model). The mode
# decides the output; the chat-model dropdown does not. So each media model id
# below maps to the mode switch to turn on, matched by visible TEXT like every
# other control here. Labels list several spellings because Alibaba has shipped
# these as "Image Generation", "Create Image" and a bare "Image" chip.

IMAGE_GEN_MODE = ("Image Generation", "Create Image", "Generate Image", "Image")
IMAGE_EDIT_MODE = ("Image Edit", "Edit Image", "Image Editing")
VIDEO_GEN_MODE = ("Video Generation", "Create Video", "Generate Video", "Video")

MEDIA_MODE_GROUPS = (IMAGE_GEN_MODE, IMAGE_EDIT_MODE, VIDEO_GEN_MODE)

# id -> {mode, aliases, desc}
IMAGE_MODELS: "dict[str, dict]" = {
    "qwen-image": {
        "mode": IMAGE_GEN_MODE,
        "aliases": ("qwen-img", "qwen-image-gen", "qwen-image-generation"),
        "desc": "Text -> image with Qwen-Image",
    },
    "qwen-image-edit": {
        "mode": IMAGE_EDIT_MODE,
        "aliases": ("qwen-edit", "qwen-image-editing"),
        "desc": "Image + text -> image with Qwen-Image-Edit (used whenever "
                "reference images are attached)",
    },
}
IMAGE_MODEL_IDS = tuple(IMAGE_MODELS)
DEFAULT_IMAGE_MODEL_ID = "qwen-image"

VIDEO_MODELS: "dict[str, dict]" = {
    "qwen-video": {
        "mode": VIDEO_GEN_MODE,
        "aliases": ("wan", "wan-video", "qwen-wan", "wanx"),
        "desc": "Text/image -> video with Alibaba's Wan model",
    },
}
VIDEO_MODEL_IDS = tuple(VIDEO_MODELS)
DEFAULT_VIDEO_MODEL_ID = "qwen-video"

# What Qwen's video mode can do: a prompt alone, or a prompt plus ONE start
# image. It has no first/last-frame, keyframe or multi-reference inputs — those
# are Seedream modes, and the video endpoint rejects them for Qwen up front.
VIDEO_MODES = (MODE_TEXT, MODE_I2V)

# Aspect ratios the video mode offers (best-effort selection; the UI decides).
ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")


def _resolve_media(requested: str, table: dict, default: str) -> str:
    req = _norm(requested)
    if not req:
        return default
    for mid, meta in table.items():
        if req == _norm(mid) or any(req == _norm(a) for a in meta.get("aliases", ())):
            return mid
    return default


def resolve_image_model_id(requested: str) -> str:
    """Canonical image-model id for a requested string, or the default."""
    return _resolve_media(requested, IMAGE_MODELS, DEFAULT_IMAGE_MODEL_ID)


def resolve_video_model_id(requested: str) -> str:
    """Canonical video-model id for a requested string, or the default."""
    return _resolve_media(requested, VIDEO_MODELS, DEFAULT_VIDEO_MODEL_ID)
