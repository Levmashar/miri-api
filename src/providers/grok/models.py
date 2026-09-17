"""
Grok CHAT-model registry — every model/mode the grok.com UI lets you pick for a
text request, exposed as an OpenAI-style model id.

Mirrors src/providers/gemini/models.py; see that file for the design notes. Grok
splits the choice across two controls, so both are represented here:
  * the model picker next to the composer  ("Grok 4.1", "Expert", "Heavy", ...)
  * mode toggles                            ("Think", "DeepSearch", ...)
A mode id layers on top of whatever model the account currently has.

Only the ids listed here switch anything; anything else leaves the UI alone.
"""

from __future__ import annotations

# The id that means "don't touch the picker" — whatever the account has selected.
DEFAULT_MODEL_ID = "grok-browser"

# id -> {labels, tool_groups, aliases, long, desc}. Same shape as the Gemini
# registry: one tool_groups entry per UI switch, its strings alternative labels.
CHAT_MODELS: "dict[str, dict]" = {
    "grok-browser": {
        "labels": (), "tool_groups": (), "aliases": ("grok", "grok-default"),
        "long": False,
        "desc": "Whatever model the account currently has selected (no switching)",
    },
    "grok-auto": {
        "labels": ("Auto", "Grok Auto"),
        "tool_groups": (),
        "aliases": (),
        "long": False,
        "desc": "Let Grok route the request to a tier itself",
    },
    "grok-fast": {
        "labels": ("Fast", "Grok 4.1 Fast", "Grok 4 Fast"),
        "tool_groups": (),
        "aliases": ("grok-4-fast", "grok-4.1-fast"),
        "long": False,
        "desc": "Fastest tier",
    },
    "grok-expert": {
        "labels": ("Expert", "Grok 4 Expert", "Grok 4.1 Expert"),
        "tool_groups": (),
        "aliases": ("grok-4-expert",),
        "long": False,
        "desc": "Reasoning tier — slower, stronger on hard problems",
    },
    "grok-heavy": {
        "labels": ("Heavy", "Grok 4 Heavy", "Grok 4.1 Heavy"),
        "tool_groups": (),
        "aliases": ("grok-4-heavy",),
        "long": True,
        "desc": "Multi-agent tier (SuperGrok Heavy); minutes per answer",
    },
    "grok-4.1": {
        "labels": ("Grok 4.1",),
        "tool_groups": (),
        "aliases": ("grok-41",),
        "long": False,
        "desc": "Grok 4.1 by name, if the picker lists versions",
    },
    "grok-4": {
        "labels": ("Grok 4",),
        "tool_groups": (),
        "aliases": (),
        "long": False,
        "desc": "Grok 4 by name, if the picker lists versions",
    },
    "grok-3": {
        "labels": ("Grok 3",),
        "tool_groups": (),
        "aliases": (),
        "long": False,
        "desc": "Grok 3 by name, where the account still offers it",
    },
    "grok-think": {
        "labels": (),
        "tool_groups": (("Think", "Thinking"),),
        "aliases": ("grok-thinking",),
        "long": False,
        "desc": "Think mode on top of the current model",
    },
    "grok-deepsearch": {
        "labels": (),
        "tool_groups": (("DeepSearch", "Deep Search"),),
        "aliases": ("grok-deep-search",),
        "long": True,
        "desc": "Agentic web search; answers in minutes",
    },
    "grok-deepersearch": {
        "labels": (),
        "tool_groups": (("DeeperSearch", "Deeper Search"),),
        "aliases": ("grok-deeper-search",),
        "long": True,
        "desc": "The longer DeepSearch variant",
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
