"""Record every model call, for monitoring in production.

A CallRecorder is passed to write_ads or search_chat for one request. It is a
LangChain callback, so it sees each call to the main model and, when that
fails, to the fallback model. The web app stores the records in model_calls
(no prompt or reply text, only what is needed to spot failures, slow calls,
cost and silent model changes) and prints one JSON line per call, which Cloud
Logging picks up.
"""
import json
import logging
import time
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("factory.model_calls")

# The recorder for the request being handled, so _structured can report a
# reply that was not valid structured output (the model call itself succeeded).
current: ContextVar["CallRecorder | None"] = ContextVar("call_recorder", default=None)


class CallRecorder(BaseCallbackHandler):
    def __init__(self):
        self.calls: list[dict] = []
        self._open: dict = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        md = metadata or {}
        provider, name = md.get("ls_provider"), md.get("ls_model_name")
        self._open[run_id] = {
            "step": md.get("step"),
            "model": f"{provider}:{name}" if provider and name else None,
            "temperature": md.get("ls_temperature"),  # None = provider default
            "started": time.monotonic(),
        }

    def _close(self, run_id, **fields) -> dict | None:
        call = self._open.pop(run_id, None)
        if call is None:
            return None
        call["latency_ms"] = int((time.monotonic() - call.pop("started")) * 1000)
        call.update(fields)
        self.calls.append(call)
        return call

    def on_llm_end(self, response, *, run_id, **kwargs):
        try:
            message = response.generations[0][0].message
        except (IndexError, AttributeError):
            message = None
        usage = getattr(message, "usage_metadata", None) or {}
        reported = (getattr(message, "response_metadata", None) or {}).get("model_name")
        self._close(run_id, ok=True, error=None, served_by=reported,
                    input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"))

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._close(run_id, ok=False, error=type(error).__name__, served_by=None,
                    input_tokens=None, output_tokens=None)

    def format_failure(self, step: str) -> None:
        """The last successful call of this step returned output that did not fit the schema."""
        for call in reversed(self.calls):
            if call["step"] == step and call["ok"]:
                call.update(ok=False, error="InvalidStructuredOutput")
                return


def emit(job: str, calls: list[dict]) -> None:
    for call in calls:
        log.info(json.dumps({"job": job, **call}, default=str))
