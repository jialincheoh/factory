"""Unit tests of the model layer that need no API key: the draft-check graph,
the search chat loop with fake chat models (including the fallback), and
photo redaction for tracing."""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.ai import draft_graph, llm
from app.ai.schemas import AdDraft, ListingAnalysis, PropertyCard, SearchFilters
from app.ai.tracing import PHOTO, redact


def analysis_with(*bodies):
    return ListingAnalysis(detected_language="English", english_description="Factory in Senai, 200A.",
                           card=PropertyCard(title="Factory", property_type="factory", deal_type="sale"),
                           drafts=[AdDraft(headline=f"Draft {i}", body=b) for i, b in enumerate(bodies)])


def run_graph(first_check, second_check=None):
    calls = []
    checks = iter([first_check, second_check])

    def analyse(description, images, n):
        return analysis_with("200A power.", "400A power and 5 min to the port."), "m:a"

    def check(analysis):
        calls.append("check")
        return next(checks), "m:a"

    def rewrite(analysis, failing):
        calls.append(("rewrite", sorted(failing)))
        return {i: AdDraft(headline="Fixed", body="200A power.") for i in failing}, "m:b"

    graph = draft_graph.build(analyse, check, rewrite)
    state = graph.invoke({"description": "x", "images": [], "n_drafts": 2})
    return state, calls


def test_clean_drafts_are_checked_once():
    state, calls = run_graph({0: [], 1: []})
    assert calls == ["check"]
    assert [c["unsupported_claims"] for c in draft_graph.draft_checks(state)] == [[], []]


def test_failing_draft_is_rewritten_once_and_original_kept():
    state, calls = run_graph({0: [], 1: ["400A power", "5 min to the port"]}, {0: [], 1: []})
    assert calls == ["check", ("rewrite", [1]), "check"]
    assert state["analysis"].drafts[1].body == "200A power."
    assert state["analysis"].drafts[0].headline == "Draft 0"  # passing drafts untouched
    checks = draft_graph.draft_checks(state)
    assert checks[1]["earlier_versions"][0]["body"] == "400A power and 5 min to the port."
    assert checks[1]["unsupported_claims"] == [] and state["models"] == ["m:a", "m:a", "m:b", "m:a"]


def test_still_failing_after_max_rewrites_is_flagged_not_looped():
    state, calls = run_graph({1: ["400A power"]}, {1: ["still 400A"]})
    assert calls.count("check") == 2 and len(calls) == 3
    assert draft_graph.draft_checks(state)[1]["unsupported_claims"] == ["still 400A"]


def test_failed_rewrite_keeps_drafts_and_flags():
    calls = []

    def analyse(description, images, n):
        return analysis_with("200A power.", "400A power."), "m:a"

    def check(analysis):
        calls.append("check")
        return {0: [], 1: ["400A power"]}, "m:a"

    def rewrite(analysis, failing):
        calls.append("rewrite")
        return {}, "rewrite failed: GoogleRateLimitError"

    state = draft_graph.build(analyse, check, rewrite).invoke({"description": "x", "images": [], "n_drafts": 2})
    assert calls == ["check", "rewrite"]  # no pointless second check
    assert state["analysis"].drafts[1].body == "400A power."
    assert draft_graph.draft_checks(state)[1]["unsupported_claims"] == ["400A power"]


def test_failed_check_leaves_drafts_unchecked():
    state, calls = run_graph(None)
    assert calls == ["check"]
    assert draft_graph.draft_checks(state)[0]["checked"] is False


class ToolFakeModel(GenericFakeChatModel):
    """Fake chat model that accepts tools (create_agent binds them)."""
    def bind_tools(self, tools, **kwargs):
        return self


class BrokenModel(ToolFakeModel):
    def _generate(self, *args, **kwargs):
        raise RuntimeError("quota exceeded")


def fake_assistant(monkeypatch, primary, fallback=None):
    models = {"fake:primary": primary, "fake:backup": fallback}
    monkeypatch.setattr(llm, "init_chat_model", lambda spec, **limits: models[spec])
    return llm.LLMAssistant("fake:primary", "RM", "fake:backup" if fallback else "")


def search_replies(model_name):
    meta = {"model_name": model_name}
    return iter([AIMessage("", tool_calls=[{"name": "search_listings", "args": {"location": "Senai"}, "id": "1"}],
                           response_metadata=meta),
                 AIMessage("Two factories in Senai match.", response_metadata=meta)])


def test_search_chat_runs_tool_loop(monkeypatch):
    assistant = fake_assistant(monkeypatch, ToolFakeModel(messages=search_replies("primary")))
    seen = []
    reply, filters, results, model = assistant.search_chat(
        [{"role": "user", "content": "factory in Senai"}], lambda f: seen.append(f) or [{"title": "A"}, {"title": "B"}])
    assert reply == "Two factories in Senai match."
    assert filters == SearchFilters(location="Senai") and len(results) == 2 and len(seen) == 1
    assert model == "fake:primary"


def test_search_chat_falls_back_and_records_model(monkeypatch):
    assistant = fake_assistant(monkeypatch, BrokenModel(messages=iter([])),
                               ToolFakeModel(messages=search_replies("backup")))
    reply, _, results, model = assistant.search_chat([{"role": "user", "content": "factory in Senai"}],
                                                     lambda f: [{"title": "A"}])
    assert reply == "Two factories in Senai match." and len(results) == 1
    assert model == "fake:backup"


def test_redact_removes_photos_but_keeps_text():
    data = {"messages": [{"content": [{"type": "text", "text": "Factory in Senai"},
                                      {"type": "image", "base64": "A" * 5000, "mime_type": "image/jpeg"}]}],
            "images": [(b"\xff\xd8 jpeg bytes", "image/jpeg")], "url": "data:image/png;base64,AAAA"}
    out = redact(data)
    content = out["messages"][0]["content"]
    assert content[0]["text"] == "Factory in Senai" and content[1]["base64"] == PHOTO
    assert out["images"][0] == [PHOTO, "image/jpeg"] and out["url"] == PHOTO
