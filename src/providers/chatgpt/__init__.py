"""Keep model-registry imports independent of the browser runtime."""


def __getattr__(name):
    if name == "ChatGPTClient":
        from src.providers.chatgpt.client import ChatGPTClient
        return ChatGPTClient
    raise AttributeError(name)
