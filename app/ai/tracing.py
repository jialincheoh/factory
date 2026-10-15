"""Optional LangSmith tracing, for local development only (see config.TRACE_TO_LANGSMITH).

Traces contain real estate agents' descriptions and buyers' chat messages.
Photos are replaced with a placeholder before anything leaves this machine.
"""
from typing import Any

PHOTO = "[photo removed before tracing]"


def redact(data: Any) -> Any:
    """Copy of data with base64 images and data: URLs replaced."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if key in ("base64", "data") and isinstance(value, str) and len(value) > 200:
                out[key] = PHOTO
            else:
                out[key] = redact(value)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(v) for v in data]
    if isinstance(data, (bytes, bytearray)):  # photo bytes in the draft graph's input
        return PHOTO
    if isinstance(data, str) and (data.startswith("data:image") or (len(data) > 2000 and " " not in data)):
        return PHOTO
    if hasattr(data, "model_dump"):  # messages and other Pydantic objects
        return redact(data.model_dump())
    return data


def make_tracer(project: str):
    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import Client

    return LangChainTracer(project_name=project, client=Client(hide_inputs=redact, hide_outputs=redact))
