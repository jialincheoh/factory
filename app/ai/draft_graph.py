"""Write ads, then check every draft against the facts before the real estate agent sees it.

    analyse ──► check ──► done
                 │  ▲
     a draft     ▼  │   at most MAX_REWRITES rounds; drafts that still fail
     fails     rewrite  are shown to the agent with a warning

The model calls are passed in as plain functions, so the graph can be tested
without a model and the same graph serves every provider.
"""
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from .schemas import AdDraft, ListingAnalysis

MAX_REWRITES = 1

# analyse(description, images, n_drafts) -> (analysis, model name)
Analyse = Callable[[str, list, int], tuple[ListingAnalysis, str]]
# check(analysis) -> ({draft index: unsupported claims}, model name); None if the check itself failed
Check = Callable[[ListingAnalysis], tuple[dict[int, list[str]] | None, str]]
# rewrite(analysis, {draft index: claims to fix}) -> ({draft index: fixed draft}, model name)
Rewrite = Callable[[ListingAnalysis, dict[int, list[str]]], tuple[dict[int, AdDraft], str]]


class DraftState(TypedDict, total=False):
    description: str
    images: list
    n_drafts: int
    analysis: ListingAnalysis
    verdicts: dict[int, list[str]] | None  # latest check; None if it failed
    history: list[list[dict]]              # per draft: earlier versions and what was wrong with them
    rewrites: int
    fixed: bool                            # the last rewrite changed at least one draft
    models: list[str]


def build(analyse: Analyse, check: Check, rewrite: Rewrite, max_rewrites: int = MAX_REWRITES):
    def analyse_node(state: DraftState) -> dict:
        analysis, model = analyse(state["description"], state["images"], state["n_drafts"])
        return {"analysis": analysis, "history": [[] for _ in analysis.drafts], "rewrites": 0, "models": [model]}

    def check_node(state: DraftState) -> dict:
        verdicts, model = check(state["analysis"])
        return {"verdicts": verdicts, "models": state["models"] + [model]}

    def rewrite_node(state: DraftState) -> dict:
        failing = {i: claims for i, claims in state["verdicts"].items() if claims}
        fixed, model = rewrite(state["analysis"], failing)
        analysis = state["analysis"].model_copy(deep=True)
        history = [list(h) for h in state["history"]]
        for i, draft in fixed.items():
            if i in failing:
                old = analysis.drafts[i]
                history[i].append({"headline": old.headline, "body": old.body, "unsupported_claims": failing[i]})
                analysis.drafts[i] = AdDraft(headline=draft.headline, body=draft.body)
        return {"analysis": analysis, "history": history, "rewrites": state["rewrites"] + 1,
                "models": state["models"] + [model], "fixed": bool(fixed)}

    def after_rewrite(state: DraftState) -> str:
        # Nothing was rewritten (the rewrite call failed): the earlier verdicts still stand.
        return "check" if state["fixed"] else END

    def after_check(state: DraftState) -> str:
        verdicts = state["verdicts"]
        if verdicts and any(verdicts.values()) and state["rewrites"] < max_rewrites:
            return "rewrite"
        return END

    graph = StateGraph(DraftState)
    graph.add_node("analyse", analyse_node)
    graph.add_node("check", check_node)
    graph.add_node("rewrite", rewrite_node)
    graph.add_edge(START, "analyse")
    graph.add_edge("analyse", "check")
    graph.add_conditional_edges("check", after_check, ["rewrite", END])
    graph.add_conditional_edges("rewrite", after_rewrite, ["check", END])
    return graph.compile()


def draft_checks(state: DraftState) -> list[dict]:
    """Per draft: whether it was checked, claims still unsupported, and earlier versions."""
    verdicts = state.get("verdicts")
    return [{"checked": verdicts is not None,
             "unsupported_claims": (verdicts or {}).get(i, []),
             "earlier_versions": state["history"][i]}
            for i in range(len(state["analysis"].drafts))]
