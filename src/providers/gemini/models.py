"""
Gemini CHAT-model registry — every model the gemini.google.com/app UI lets you
pick for a text request, exposed as an OpenAI-style model id.

Same two jobs as the Seedream registry (src/providers/seedream/models.py):
  1. Map the API `model` id a caller sends -> the exact UI LABEL the browser
     client must click in the model picker.
  2. Say which ids are MODES rather than picker entries (Deep Research is a
     composer tool layered on top of the current model, not a menu item), and
     which ones are slow enough to need the long-mode deadline.

Labels are matched by TEXT, tolerantly (exact -> startswith -> contains, see
src/providers/model_picker.py), and several aliases are listed per model on
purpose: Google renames the picker entries between releases ("2.5 Pro" ->
"Thinking" -> "Gemini 3 Pro") far more often than it changes what they mean.
An entry a given account's plan does not offer is simply not selected — the
request still runs on whatever model the UI already had.
"""

from __future__ import annotations

# The id that means "don't touch the picker" — whatever the account has selected.
DEFAULT_MODEL_ID = "gemini-browser"

# id -> {labels, tool_groups, aliases, long, desc}
#   labels      — model-picker entries to click, tried in order
#   tool_groups — composer tool/mode switches to enable on top of the current
#                 model. ONE GROUP PER SWITCH; the strings inside a group are
#                 alternative labels for that same switch, tried in order.
#   aliases — extra API ids that resolve here (version-explicit spellings)
#   long    — needs the long-mode deadline (Config.LONG_MODE_TIMEOUT)
CHAT_MODELS: "dict[str, dict]" = {
    "gemini-browser": {
        "labels": (), "tool_groups": (), "aliases": ("gemini", "gemini-default"),
        "long": False,
        "desc": "Whatever model the account currently has selected (no switching)",
    },
    "gemini-fast": {
        "labels": ("Fast", "Gemini 3 Fast", "3 Fast", "Flash", "2.5 Flash",
                   "Gemini 2.5 Flash"),
        "tool_groups": (),
        "aliases": ("gemini-flash", "gemini-3-flash", "gemini-2.5-flash"),
        "long": False,
        "desc": "Quickest replies (the Flash/Fast tier)",
    },
    "gemini-balanced": {
        "labels": ("Balanced", "Gemini 3 Balanced", "3 Balanced", "Auto"),
        "tool_groups": (),
        "aliases": ("gemini-auto",),
        "long": False,
        "desc": "The middle tier where the app offers one",
    },
    "gemini-thinking": {
        "labels": ("Thinking", "Gemini 3 Thinking", "3 Thinking",
                   "2.5 Pro (Thinking)"),
        "tool_groups": (),
        "aliases": ("gemini-3-thinking", "gemini-think"),
        "long": False,
        "desc": "Reasoning tier — slower, better on hard problems",
    },
    "gemini-pro": {
        "labels": ("Gemini 3 Pro", "3 Pro", "Pro", "Gemini 2.5 Pro", "2.5 Pro"),
        "tool_groups": (),
        "aliases": ("gemini-3-pro", "gemini-2.5-pro"),
        "long": False,
        "desc": "The Pro tier (paid Google AI plans)",
    },
    "gemini-deep-think": {
        "labels": ("Deep Think", "Deep Think (Ultra)", "DeepThink"),
        "tool_groups": (),
        "aliases": ("gemini-ultra", "gemini-deepthink"),
        "long": True,
        "desc": "Extended reasoning — Google AI Ultra only, minutes per answer",
    },
    "gemini-deep-research": {
        # A composer TOOL, not a picker entry: it plans first, then researches
        # once "Start research" is confirmed (the client clicks that for you).
        "labels": (),
        "tool_groups": (("Deep Research", "Deep research"),),
        "aliases": ("gemini-research",),
        "long": True,
        "desc": "Multi-step web research; answers in minutes, not seconds",
    },
}

CHAT_MODEL_IDS = tuple(CHAT_MODELS)

# Buttons that confirm a planned research run before it actually starts.
START_RESEARCH_LABELS = ("Start research", "Start Research", "Begin research")


def _norm(s: str) -> str:
    """Normalise a model id for tolerant matching (dots/dashes/spaces/case)."""
    return (s or "").strip().lower().replace("_", "-").replace(" ", "-").replace(".", "-")


_NORM_INDEX: "dict[str, str]" = {}
for _mid, _meta in CHAT_MODELS.items():
    _NORM_INDEX[_norm(_mid)] = _mid
    for _alias in _meta.get("aliases", ()):
        _NORM_INDEX.setdefault(_norm(_alias), _mid)


def resolve_chat_model_id(requested: str) -> str:
    """Canonical chat-model id for a requested string, or "" if unrecognised.

    "" (and the default id) mean "leave the picker alone" — an unknown id must
    never silently switch the account to some other model.
    """
    req = _norm(requested)
    if not req:
        return ""
    if req in _NORM_INDEX:
        return _NORM_INDEX[req]
    # Match on the UI label too ("gemini-deep-think" vs label "Deep Think").
    for mid, meta in CHAT_MODELS.items():
        if any(_norm(lbl) == req for lbl in meta.get("labels", ())):
            return mid
        if any(_norm(t) == req for grp in meta.get("tool_groups", ()) for t in grp):
            return mid
    return ""


def label_candidates(model_id: str) -> list:
    """Model-picker labels to try when selecting `model_id`."""
    return list(CHAT_MODELS.get(model_id, {}).get("labels", ()))


def tool_groups(model_id: str) -> list:
    """Composer switches to enable for `model_id`, one label-group per switch."""
    return [list(grp) for grp in CHAT_MODELS.get(model_id, {}).get("tool_groups", ())]


def all_tool_groups() -> list:
    """Every switch this registry can drive, de-duplicated.

    Used once per tab to CLEAR modes the UI kept from an earlier session: the
    browser profile is persistent, so a mode switched on days ago is still on,
    and this client has no memory of it. Clearing is safe because set_mode()
    only clicks a switch OFF when the page positively reports it as on.
    """
    seen, out = set(), []
    for meta in CHAT_MODELS.values():
        for grp in meta.get("tool_groups", ()):
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
