"""
DeepSeek CHAT-model registry — the model choices chat.deepseek.com offers for a
text request, exposed as OpenAI-style model ids.

Same shape as the Gemini/Grok registries (see src/providers/gemini/models.py for
the design notes). DeepSeek is the simplest case in this repo: there is no model
DROPDOWN at all. The choice is two composer switches —

    DeepThink   the reasoning model (R1 lineage) instead of the fast chat model
    Search      web search grounding

— which are independent and can both be on, so the registry exposes each one and
the combination. Everything is expressed as `tool_groups`: one group per switch,
its strings alternative labels for that same switch (DeepSeek has shipped it as
both "DeepThink" and "DeepThink (R1)").

Because switches are STICKY per tab, `deepseek-browser` is not merely "do
nothing": the client also switches OFF anything a previous request on that tab
turned on. See apply_modes() in src/providers/model_picker.py.
"""

from __future__ import annotations

# The id that means "plain chat" — no reasoning, no search.
DEFAULT_MODEL_ID = "deepseek-browser"

_THINK = ("DeepThink", "DeepThink (R1)", "Deep Think")
_SEARCH = ("Search", "Web Search", "Search the web")

# id -> {labels, tool_groups, aliases, long, desc}
CHAT_MODELS: "dict[str, dict]" = {
    "deepseek-browser": {
        "labels": (),
        "tool_groups": (),
        "aliases": ("deepseek", "deepseek-chat", "deepseek-v3", "deepseek-default"),
        "long": False,
        "desc": "Plain chat — both composer switches off",
    },
    "deepseek-think": {
        "labels": (),
        "tool_groups": (_THINK,),
        "aliases": ("deepseek-r1", "deepseek-reasoner", "deepseek-deepthink",
                    "deepseek-thinking"),
        "long": False,
        "desc": "DeepThink — the reasoning model; slower, shows its working",
    },
    "deepseek-search": {
        "labels": (),
        "tool_groups": (_SEARCH,),
        "aliases": ("deepseek-web", "deepseek-websearch"),
        "long": False,
        "desc": "Web search grounding on the fast chat model",
    },
    "deepseek-think-search": {
        "labels": (),
        "tool_groups": (_THINK, _SEARCH),
        "aliases": ("deepseek-r1-search", "deepseek-search-think",
                    "deepseek-reasoner-search"),
        "long": True,
        "desc": "DeepThink + web search — the slowest, most thorough combination",
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
    """Model-picker labels to try when selecting `model_id` (DeepSeek: none)."""
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
