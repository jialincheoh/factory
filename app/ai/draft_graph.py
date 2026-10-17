"""Write ads, then check every draft against the facts before the real estate agent sees it.

    analyse ─┬─► check draft 1 ─┐
             ├─► check draft 2 ─┼─► review ─┬─► rewrite draft k ─┐
             └─► check draft 3 ─┘     ▲     │   (only drafts      ├─► apply ─► check the
                                      │     │    that failed)    ─┘           rewritten drafts
                                      └─────┴── done when every draft passes, or after
                                                MAX_REWRITES rounds (still-failing drafts
                                                are shown to the agent with a warning)

Each draft is checked and fixed in its own branch (LangGraph Send), so the
branches run at the same time and each check sees only one draft. The model
calls are passed in as plain functions, so the graph can be tested without a
model and the same graph serves every provider.
"""
import operator
from typing import Annotated, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .schemas import AdDraft, ListingAnalysis

MAX_REWRITES = 1

# analyse(description, images, n_drafts) -> (analysis, model name)
Analyse = Callable[[str, list, int], tuple[ListingAnalysis, str]]
# check(analysis, draft index) -> (unsupported claims, model name); claims None if the check itself failed
Check = Callable[[ListingAnalysis, int], tuple[list[str] | None, str]]
# rewrite(analysis, draft index, claims to fix) -> (fixed draft, model name); draft None if the rewrite failed
Rewrite = Callable[[ListingAnalysis, int, list[str]], tuple[AdDraft | None, str]]


def _merge(a: dict, b: dict) -> dict:
    return {**a, **b}


class DraftState(TypedDict, total=False):
    description: str
    images: list
    n_drafts: int
    analysis: ListingAnalysis
    verdicts: Annotated[dict[int, list[str] | None], _merge]  # latest check per draft; None if it failed
    fixes: Annotated[dict[int, AdDraft | None], _merge]       # this round's rewrites, keyed by draft
    history: list[list[dict]]              # per draft: earlier versions and what was wrong with them
    rewrites: int
    models: Annotated[list[str], operator.add]


class DraftTask(TypedDict, total=False):
    """What one parallel branch receives."""
    i: int
    analysis: ListingAnalysis
    claims: list[str]


def build(analyse: Analyse, check: Check, rewrite: Rewrite, max_rewrites: int = MAX_REWRITES, checkpointer=None):
    def analyse_node(state: DraftState) -> dict:
        analysis, model = analyse(state["description"], state["images"], state["n_drafts"])
        # Photos are only needed here; dropping them keeps later state (and any checkpoint) small.
        return {"analysis": analysis, "images": [], "history": [[] for _ in analysis.drafts], "rewrites": 0,
                "models": [model]}

    def check_one(task: DraftTask) -> dict:
        claims, model = check(task["analysis"], task["i"])
        return {"verdicts": {task["i"]: claims}, "models": [model]}

    def rewrite_one(task: DraftTask) -> dict:
        fixed, model = rewrite(task["analysis"], task["i"], task["claims"])
        return {"fixes": {task["i"]: fixed}, "models": [model]}

    def review(state: DraftState) -> dict:
        return {"fixes": {i: None for i in state.get("fixes") or {}}}  # clear last round's fixes

    def apply(state: DraftState) -> dict:
        analysis = state["analysis"].model_copy(deep=True)
        history = [list(h) for h in state["history"]]
        for i, draft in state["fixes"].items():
            if draft is None:
                continue  # the rewrite failed: keep the draft and its verdict
            old = analysis.drafts[i]
            history[i].append({"headline": old.headline, "body": old.body,
                               "unsupported_claims": state["verdicts"][i]})
            analysis.drafts[i] = AdDraft(headline=draft.headline, body=draft.body, cover_photo=old.cover_photo)
        return {"analysis": analysis, "history": history, "rewrites": state["rewrites"] + 1}

    def check_all(state: DraftState):
        return [Send("check_one", {"i": i, "analysis": state["analysis"]}) for i in range(len(state["analysis"].drafts))]

    def after_review(state: DraftState):
        failing = {i: c for i, c in state["verdicts"].items() if c}
        if failing and state["rewrites"] < max_rewrites:
            return [Send("rewrite_one", {"i": i, "analysis": state["analysis"], "claims": c}) for i, c in failing.items()]
        return END

    def after_apply(state: DraftState):
        fixed = [i for i, d in state["fixes"].items() if d is not None]
        # Nothing was rewritten (every rewrite failed): the earlier verdicts still stand.
        return [Send("check_one", {"i": i, "analysis": state["analysis"]}) for i in fixed] or END

    graph = StateGraph(DraftState)
    graph.add_node("analyse", analyse_node)
    graph.add_node("check_one", check_one)
    graph.add_node("review", review)
    graph.add_node("rewrite_one", rewrite_one)
    graph.add_node("apply", apply)
    graph.add_edge(START, "analyse")
    graph.add_conditional_edges("analyse", check_all, ["check_one"])
    graph.add_edge("check_one", "review")
    graph.add_conditional_edges("review", after_review, ["rewrite_one", END])
    graph.add_edge("rewrite_one", "apply")
    graph.add_conditional_edges("apply", after_apply, ["check_one", END])
    return graph.compile(checkpointer=checkpointer)


def draft_checks(state: DraftState) -> list[dict]:
    """Per draft: whether it was checked, claims still unsupported, and earlier versions."""
    verdicts = state.get("verdicts") or {}
    return [{"checked": verdicts.get(i) is not None,
             "unsupported_claims": verdicts.get(i) or [],
             "earlier_versions": state["history"][i]}
            for i in range(len(state["analysis"].drafts))]


def describe_step(node: str, update: dict, checks_so_far: dict[int, int] | None = None,
                  max_rewrites: int = MAX_REWRITES) -> str | None:
    """One line for the agent's progress view, from a graph update (used when streaming).
    checks_so_far counts earlier checks per draft, so a draft that still fails after the last
    allowed rewrite is reported as flagged, not as going back to the editor."""
    if node == "analyse":
        a = update["analysis"]
        return f"Read the description and photos, filled the property card and wrote {len(a.drafts)} drafts."
    if node == "check_one":
        (i, claims), = update["verdicts"].items()
        seen = checks_so_far if checks_so_far is not None else {}
        final_round = seen.get(i, 0) >= max_rewrites
        seen[i] = seen.get(i, 0) + 1
        if claims and final_round:
            return f"Draft {i + 1}: {len(claims)} claim(s) still not supported; it will be shown to you with a warning."
        if claims is None:
            return f"Draft {i + 1}: the fact check could not run; please review it closely."
        return (f"Draft {i + 1}: every claim is supported." if not claims
                else f"Draft {i + 1}: {len(claims)} claim(s) not supported by the facts, sending it to the editor.")
    if node == "rewrite_one":
        (i, draft), = update["fixes"].items()
        return f"Draft {i + 1}: edited." if draft is not None else f"Draft {i + 1}: the editor could not fix it."
    return None
