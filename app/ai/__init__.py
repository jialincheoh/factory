from functools import lru_cache

from .. import config


@lru_cache(maxsize=1)
def get_assistant():
    if config.LLM_MODEL == "offline":
        from .offline import OfflineAssistant
        return OfflineAssistant(config.CURRENCY)
    from .llm import LLMAssistant
    return LLMAssistant(config.LLM_MODEL, config.CURRENCY)
