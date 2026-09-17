"""ChatGPT picker entries, using the same registry contract as Qwen/DeepSeek.

Availability depends on the signed-in account. Missing entries keep the current
model, like the other browser providers; these are UI choices, not API models.
"""

DEFAULT_MODEL_ID = "chatgpt-browser"
CHAT_MODELS = {
    DEFAULT_MODEL_ID: {"labels": (), "long": False, "desc": "Current account selection"},
    "chatgpt-auto": {"labels": ("Auto", "Авто"), "long": False, "desc": "Automatic selection"},
    "chatgpt-instant": {"labels": ("Instant", "Мгновенно"), "long": False, "desc": "Instant replies"},
    "chatgpt-thinking": {"labels": ("Thinking", "Думающая"), "long": True, "desc": "Thinking"},
    "chatgpt-pro": {"labels": ("Pro",), "long": True, "desc": "Pro (requires account access)"},
}

# Versioned entries are tried by their exact names, including the Legacy models
# submenu. Never map a versioned request to a different generation of a model.
for _id, _labels, _long in (
    ("gpt-6-astra", ("GPT-6 Astra", "Astra"), True),
    ("gpt-5.6-sol", ("GPT-5.6 Sol", "5.6 Sol"), True),
    ("gpt-5.6-terra", ("GPT-5.6 Terra", "5.6 Terra"), True),
    ("gpt-5.6-luna", ("GPT-5.6 Luna", "5.6 Luna"), False),
    ("gpt-5.5", ("GPT-5.5", "5.5"), True),
    ("gpt-5.4", ("GPT-5.4", "5.4"), True),
    ("gpt-5.2", ("GPT-5.2", "5.2"), True),
    ("gpt-5.1", ("GPT-5.1", "5.1"), True),
    ("gpt-5", ("GPT-5",), True),
    ("gpt-4o", ("GPT-4o", "4o"), False),
    ("gpt-4.1", ("GPT-4.1", "4.1"), False),
    ("gpt-4.5", ("GPT-4.5", "4.5"), False),
    ("o3", ("o3",), True),
    ("o3-pro", ("o3-pro",), True),
    ("o4-mini", ("o4-mini",), True),
    ("o4-mini-high", ("o4-mini-high",), True),
):
    CHAT_MODELS["chatgpt-" + _id] = {
        "labels": _labels, "aliases": (_id,), "long": _long,
        "desc": _labels[0] + " (where offered in the account picker)",
    }

CHAT_MODEL_IDS = tuple(CHAT_MODELS)


def resolve_chat_model_id(requested):
    raw = (requested or "").strip().lower().replace("_", "-")
    for mid, meta in CHAT_MODELS.items():
        if raw in (mid, *meta.get("aliases", ())):
            return mid
    return ""


def label_candidates(model_id):
    return list(CHAT_MODELS.get(model_id, {}).get("labels", ()))


def is_long(model_id):
    return bool(CHAT_MODELS.get(model_id, {}).get("long"))


def describe(model_id):
    return CHAT_MODELS.get(model_id, {}).get("desc", "")
