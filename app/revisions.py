"""Agent feedback on a draft, run through the revision graph (ai/revise_graph.py).

The graph pauses between pieces of feedback; LangGraph's Postgres checkpointer
saves where it paused, in the same database, so the next piece of feedback
resumes the same thread even after a restart. Every revision is also written
to draft_revisions, which is what the review page shows and what research reads.
"""
import threading
import uuid

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.types.json import Jsonb

from . import config
from .ai import get_assistant, pii, revise_graph

MAX_FEEDBACK = 500
_setup_done = False
_setup_lock = threading.Lock()


def ensure_setup() -> None:
    """Create LangGraph's checkpoint tables once per process. Must run outside any open transaction:
    the setup builds an index CONCURRENTLY, which waits for every open transaction to finish."""
    global _setup_done
    if _setup_done:
        return
    with _setup_lock:
        if not _setup_done:
            with PostgresSaver.from_conn_string(config.DATABASE_URL) as saver:
                saver.setup()
            _setup_done = True


def _saver():
    """A checkpointer on its own connection (it needs autocommit)."""
    cm = PostgresSaver.from_conn_string(config.DATABASE_URL)
    return cm, cm.__enter__()


def current_drafts(conn, request_id) -> dict[int, dict]:
    """Drafts in generated order, with the latest revision applied: {generated index: draft}."""
    rows = conn.execute("""
        SELECT c.candidate_id, (c.raw_response->>'generated_index')::int AS gi, c.cover_position,
               coalesce(r.headline, c.headline) AS headline, coalesce(r.ad_text, c.ad_text) AS ad_text
        FROM candidates c
        LEFT JOIN LATERAL (SELECT headline, ad_text FROM draft_revisions r
                           WHERE r.candidate_id = c.candidate_id AND r.ok ORDER BY created_at DESC LIMIT 1) r ON true
        WHERE c.request_id = %s ORDER BY gi""", [request_id]).fetchall()
    return {r["gi"]: r for r in rows}


def revise(conn, request_id, candidate_id, feedback: str) -> dict:
    """Apply one piece of agent feedback to one draft. Returns the stored revision."""
    feedback = pii.mask(feedback.strip())[:MAX_FEEDBACK]
    drafts = current_drafts(conn, request_id)
    gi = next(i for i, d in drafts.items() if d["candidate_id"] == candidate_id)
    assistant = get_assistant()
    cm, saver = _saver()
    try:
        graph = revise_graph.build(assistant.revise, assistant.check_draft, checkpointer=saver)
        cfg = {"configurable": {"thread_id": f"revise:{request_id}"}}
        if not graph.get_state(cfg).values:  # first feedback on this request: start the thread
            req = conn.execute("SELECT analysis FROM generation_requests WHERE request_id = %s", [request_id]).fetchone()
            analysis = dict(req["analysis"] or {})
            analysis["drafts"] = [{"headline": d["headline"], "body": d["ad_text"], "cover_photo": d["cover_position"]}
                                  for _, d in sorted(drafts.items())]
            graph.invoke({"analysis": analysis}, cfg)  # runs up to the pause
        state = graph.invoke(Command(resume={"draft": gi, "text": feedback}), cfg)
    finally:
        cm.__exit__(None, None, None)
    last = state["last"]
    row = {"revision_id": uuid.uuid4(), "ok": last["ok"], "headline": last.get("headline"),
           "ad_text": last.get("body"), "unsupported_claims": last.get("unsupported_claims"),
           "model_name": last.get("model")}
    conn.execute(
        "INSERT INTO draft_revisions (revision_id, request_id, candidate_id, feedback, ok, headline, ad_text, "
        "unsupported_claims, model_name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [row["revision_id"], request_id, candidate_id, feedback, row["ok"], row["headline"], row["ad_text"],
         Jsonb(row["unsupported_claims"]) if row["unsupported_claims"] is not None else None, row["model_name"]])
    return row
