from functools import lru_cache

from .. import config


@lru_cache(maxsize=1)
def get_assistant():
    if config.LLM_MODEL == "offline":
        from .offline import OfflineAssistant
        return OfflineAssistant(config.CURRENCY)
    from .llm import LLMAssistant
    tracer = None
    if config.TRACE_TO_LANGSMITH:
        from .tracing import make_tracer
        tracer = make_tracer(config.LANGSMITH_PROJECT)
    return LLMAssistant(config.LLM_MODEL, config.CURRENCY, config.LLM_FALLBACK_MODEL, tracer)
