"""Human-in-the-loop revision of drafts, after the agent has seen them.

    START ─► wait for feedback ─► revise the draft ─► check it ─┐
                  ▲   (interrupt: the graph pauses here,        │
                  │    saved in Postgres, until the agent       │
                  │    sends feedback on the review page)       │
                  └──────────────────────────────────────────────┘

One thread per generation request (thread id = request id). The checkpointer
keeps the graph's state between page loads and server restarts, so each piece
of feedback builds on the previous revision. The model calls are passed in as
plain functions, like the draft graph, so tests need no model.

The state holds plain dicts (no photos, no Pydantic objects), so checkpoints
stay small and deserialise without registering types.
"""
import operator
from typing import Annotated, Callable, TypedDict

from langgraph.graph import START, StateGraph
from langgraph.types import interrupt

from .schemas import AdDraft, ListingAnalysis

# revise(analysis, draft index, feedback) -> (revised draft or None if the call failed, model name)
Revise = Callable[[ListingAnalysis, int, str], tuple[AdDraft | None, str]]
# check(analysis, draft index) -> (unsupported claims or None if the check failed, model name)
Check = Callable[[ListingAnalysis, int], tuple[list[str] | None, str]]


class ReviseState(TypedDict, total=False):
    analysis: dict                 # ListingAnalysis.model_dump(), drafts in generated order
    feedback: dict                 # {"draft": index, "text": feedback} being worked on
    last: dict                     # result of the latest revision, for the web app
    revisions: Annotated[list[dict], operator.add]


def build(revise: Revise, check: Check, checkpointer=None):
    def wait_for_feedback(state: ReviseState) -> dict:
        # Pauses the run. The web app resumes it with Command(resume={"draft": i, "text": "..."}).
        feedback = interrupt({"drafts": [{"headline": d["headline"], "body": d["body"]}
                                         for d in state["analysis"]["drafts"]]})
        return {"feedback": feedback}

    def revise_node(state: ReviseState) -> dict:
        i, text = state["feedback"]["draft"], state["feedback"]["text"]
        analysis = ListingAnalysis.model_validate(state["analysis"])
        draft, model = revise(analysis, i, text)
        if draft is None:
            return {"last": {"draft": i, "ok": False, "model": model}}
        old = analysis.drafts[i]
        analysis.drafts[i] = AdDraft(headline=draft.headline, body=draft.body, cover_photo=old.cover_photo)
        return {"analysis": analysis.model_dump(),
                "last": {"draft": i, "ok": True, "model": model, "headline": draft.headline, "body": draft.body}}

    def check_node(state: ReviseState) -> dict:
        last = dict(state["last"])
        if last["ok"]:
            claims, model = check(ListingAnalysis.model_validate(state["analysis"]), last["draft"])
            last.update(unsupported_claims=claims, check_model=model)
        return {"last": last, "revisions": [{**last, "feedback": state["feedback"]["text"]}]}

    graph = StateGraph(ReviseState)
    graph.add_node("wait_for_feedback", wait_for_feedback)
    graph.add_node("revise", revise_node)
    graph.add_node("check", check_node)
    graph.add_edge(START, "wait_for_feedback")
    graph.add_edge("wait_for_feedback", "revise")
    graph.add_edge("revise", "check")
    graph.add_edge("check", "wait_for_feedback")
    return graph.compile(checkpointer=checkpointer)
