"""
Seedream (Dreamina) video-model registry + generation modes.

Two jobs:
  1. Map the API `model` id a caller sends  ->  the exact UI dropdown LABEL the
     browser client must click, plus which special ref-modes that model supports.
  2. Define the generation MODES and how each maps to Dreamina's reference-mode
     dropdown value (omni | frames | multiframes).

Capability flags are informational (drawn from Dreamina's docs + third-party
model catalogs). They drive validation warnings and /v1/models, NOT hard gates —
the live UI is the real authority on what a given account/plan can do, and model
selection degrades gracefully when a label isn't offered.
"""

from __future__ import annotations

# ── Generation modes ────────────────────────────────────────────
MODE_TEXT = "text_to_video"
MODE_I2V = "image_to_video"
MODE_FIRST_LAST = "first_last_frame"
MODE_MULTIFRAME = "multiframe"
MODE_OMNI = "omni_reference"

MODES = (MODE_TEXT, MODE_I2V, MODE_FIRST_LAST, MODE_MULTIFRAME, MODE_OMNI)

# How many images each mode expects (min, max). None max = unbounded-ish.
MODE_IMAGE_BOUNDS = {
    MODE_TEXT: (0, 0),
    MODE_I2V: (1, 1),
    MODE_FIRST_LAST: (1, 2),   # first (+ optional last)
    MODE_MULTIFRAME: (2, 10),  # 2–10 keyframes
    MODE_OMNI: (1, 12),        # up to ~12 references (image/video/audio)
}

# Mode -> Dreamina's reference-mode dropdown value. Modes with no image input
# (text/image-to-video) don't touch the reference-mode selector.
MODE_TO_REF_MODE = {
    MODE_FIRST_LAST: "frames",
    MODE_MULTIFRAME: "multiframes",
    MODE_OMNI: "omni",
}

# Candidate visible labels for the reference-mode dropdown option, since the UI
# text may differ from the internal value. Tried in order (best-effort match).
REF_MODE_LABELS = {
    "frames": ("first and last frame", "first/last frame", "first & last", "frames"),
    "multiframes": ("multi-frame", "multiframe", "multiframes", "keyframe"),
    "omni": ("omni reference", "omni", "reference"),
}


# ── Video models ────────────────────────────────────────────────
# id -> {label, first_last, multiframe, omni}. `label` is the text to select in
# the model dropdown. Order here is the display/precedence order.
VIDEO_MODELS: "dict[str, dict]" = {
    "seedance-2.5":       {"label": "Seedance 2.5",       "first_last": True,  "multiframe": True,  "omni": True},
    "seedance-2.0":       {"label": "Seedance 2.0",       "first_last": True,  "multiframe": True,  "omni": True},
    "seedance-2.0-fast":  {"label": "Seedance 2.0 Fast",  "first_last": True,  "multiframe": True,  "omni": True},
    "seedance-2.0-mini":  {"label": "Seedance 2.0 Mini",  "first_last": True,  "multiframe": True,  "omni": True},
    "seedance-1.5-pro":   {"label": "Seedance 1.5 Pro",   "first_last": False, "multiframe": False, "omni": False},
    "seedance-1.0-pro":   {"label": "Seedance 1.0 Pro",   "first_last": False, "multiframe": False, "omni": False},
    "seedance-1.0-lite":  {"label": "Seedance 1.0 Lite",  "first_last": False, "multiframe": False, "omni": False},
    "video-3.0":          {"label": "Video 3.0",          "first_last": True,  "multiframe": True,  "omni": False},
    "video-3.0-pro":      {"label": "Video 3.0 Pro",      "first_last": True,  "multiframe": True,  "omni": False},
    "video-s2.0-pro":     {"label": "Video S2.0 Pro",     "first_last": False, "multiframe": False, "omni": False},
    "video-1.0":          {"label": "Video 1.0",          "first_last": False, "multiframe": False, "omni": False},
}

DEFAULT_MODEL_ID = "seedance-2.0"


def _norm(s: str) -> str:
    """Normalise a model id for tolerant matching (dots/dashes/spaces/case)."""
    return (s or "").strip().lower().replace("_", "-").replace(" ", "-").replace(".", "-")


_NORM_INDEX = {_norm(k): k for k in VIDEO_MODELS}


def resolve_model_id(requested: str) -> str:
    """Canonical video-model id for a requested string, or the default.

    Tolerant: "Seedance 2.0", "seedance-2-0", "seedance_2.0" all resolve to
    "seedance-2.0". Unknown -> DEFAULT_MODEL_ID."""
    req = _norm(requested)
    if not req:
        return DEFAULT_MODEL_ID
    if req in _NORM_INDEX:
        return _NORM_INDEX[req]
    # match on the UI label too ("seedance-2-0-fast" vs label "Seedance 2.0 Fast")
    for mid, meta in VIDEO_MODELS.items():
        if _norm(meta["label"]) == req:
            return mid
    return DEFAULT_MODEL_ID


def label_for(model_id: str) -> str:
    meta = VIDEO_MODELS.get(model_id)
    return meta["label"] if meta else ""


def supports(model_id: str, mode: str) -> bool:
    """Whether `model_id` is documented to support `mode` (best-effort)."""
    meta = VIDEO_MODELS.get(model_id)
    if not meta:
        return True  # unknown model — don't block, let the UI decide
    if mode == MODE_FIRST_LAST:
        return meta["first_last"]
    if mode == MODE_MULTIFRAME:
        return meta["multiframe"]
    if mode == MODE_OMNI:
        return meta["omni"]
    return True  # text/image-to-video: available on essentially every model


# ── Image models ────────────────────────────────────────────────
# API slug -> candidate dropdown-label substrings, tried in order (exact then
# substring). In Dreamina's IMAGE picker the ByteDance models read "Image X.Y"
# (subtitle "by Seedream X.Y"); we list several aliases so the Pro/Lite labelling
# and version drift don't break selection. Only Seedream-branded models live here
# (Nano Banana / GPT Image are piped into Dreamina but belong to other providers).
IMAGE_MODELS: "dict[str, tuple]" = {
    "seedream-5.0-pro":  ("Image 5.0 Pro", "Seedream 5.0 Pro", "Image 5.0", "5.0 Pro"),
    "seedream-5.0-lite": ("Image 5.0 Lite", "Seedream 5.0 Lite", "5.0 Lite"),
    "seedream-4.5":      ("Image 4.5", "Seedream 4.5"),
    "seedream-4.0":      ("Image 4.0", "Seedream 4.0"),
    "seedream-3.0":      ("Image 3.0", "Seedream 3.0"),
}

DEFAULT_IMAGE_MODEL_ID = "seedream-4.5"

_IMG_NORM_INDEX = {_norm(k): k for k in IMAGE_MODELS}


def resolve_image_model_id(requested: str) -> str:
    """Canonical image-model id for a requested string, or the default."""
    req = _norm(requested)
    if not req:
        return DEFAULT_IMAGE_MODEL_ID
    if req in _IMG_NORM_INDEX:
        return _IMG_NORM_INDEX[req]
    for mid, labels in IMAGE_MODELS.items():
        if any(_norm(l) == req for l in labels):
            return mid
    return DEFAULT_IMAGE_MODEL_ID


def image_label_candidates(model_id: str) -> list:
    """UI dropdown-label candidates to try when selecting `model_id`."""
    return list(IMAGE_MODELS.get(model_id, ()))
