"""What gets recorded about use of the site: buyer events, searches and every model call."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import Request
from psycopg.types.json import Jsonb

from .. import config, db
from ..ai import get_assistant, pii
from ..ai.calllog import CallRecorder, emit
from ..ai.llm import AD_PROMPT_FINGERPRINT, PROMPT_VERSION, SEARCH_PROMPT_FINGERPRINT, SEARCH_PROMPT_VERSION
from ..ai.schemas import SearchFilters

log = logging.getLogger("factory.tracking")


def visitor_id(request: Request) -> str:
    """Random ID kept in the session cookie for one visitor; never linked to an account or contact details."""
    vid = request.session.get("vid")
    if not vid:
        vid = request.session["vid"] = str(uuid.uuid4())
    return vid


def log_events(conn, ad_ids, event_type: str, vid: str | None = None) -> None:
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ad_events (ad_id, event_type, occurred_at, source, source_event_id, visitor_id) "
            "VALUES (%s, %s, %s, 'portal', %s, %s)",
            [(ad_id, event_type, now, uuid.uuid4().hex, vid) for ad_id in ad_ids],
        )


def log_search(
    conn,
    vid: str,
    method: str,
    filters: SearchFilters,
    result_count: int | None,
    query: str | None = None,
    interpreter: str | None = None,
    declined: str | None = None,
) -> int | None:
    """One row per search: how it was made, what was typed (contact details masked), the filters it
    became, and the result count. Returns the search_id, or None when search logging is off."""
    if not config.SEARCH_LOGGING:
        return None
    row = conn.execute(
        "INSERT INTO search_events (visitor_id, method, query_text, filters, result_count, interpreter, declined) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING search_id",
        [
            vid,
            method,
            pii.mask(query)[:1000] if query else None,
            Jsonb(filters.model_dump(exclude_none=True, exclude_defaults=True)),
            result_count,
            interpreter,
            declined,
        ],
    ).fetchone()
    return row["search_id"]


def log_model_calls(conn, job: str, recorder: CallRecorder, request_id=None, search_id=None) -> None:
    """One model_calls row per model call, for monitoring (see scripts/monitor.py)."""
    assistant = get_assistant()
    version, fingerprint = (
        (PROMPT_VERSION, AD_PROMPT_FINGERPRINT)
        if job == "write_ads"
        else (SEARCH_PROMPT_VERSION, SEARCH_PROMPT_FINGERPRINT)
    )
    emit(job, recorder.calls)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO model_calls (job, request_id, search_id, step, model, served_by, is_fallback, ok, error, "
            "latency_ms, input_tokens, output_tokens, prompt_version, prompt_hash, settings) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    job,
                    request_id,
                    search_id,
                    c["step"],
                    c["model"],
                    c["served_by"],
                    bool(c["model"]) and c["model"] != assistant.model_name,
                    c["ok"],
                    c["error"],
                    c["latency_ms"],
                    c["input_tokens"],
                    c["output_tokens"],
                    version,
                    fingerprint,
                    Jsonb(assistant.settings()),
                )
                for c in recorder.calls
            ],
        )


def log_failed_calls(job: str, recorder: CallRecorder, **ids) -> None:
    """Record the calls of a request that failed, outside its rolled-back transaction."""
    try:
        with db.connect() as conn:
            log_model_calls(conn, job, recorder, **ids)
    except Exception:
        log.exception("could not record the model calls of a failed %s", job)
