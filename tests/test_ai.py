"""Unit tests of the model layer that need no API key: the draft-check graph,
the search chat loop with fake chat models (including the fallback), and
photo redaction for tracing."""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.ai import calllog, draft_graph, llm, pii
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
        a = analysis_with("200A power.", "400A power and 5 min to the port.")
        a.drafts[1].cover_photo = 2
        return a, "m:a"

    def check(analysis):
        calls.append("check")
        return next(checks), "m:a"

    def rewrite(analysis, failing):
        calls.append(("rewrite", sorted(failing)))
        return {i: AdDraft(headline="Fixed", body="200A power.") for i in failing}, "m:b"  # no cover from the editor

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
    assert state["analysis"].drafts[1].cover_photo == 2  # the rewrite keeps the writer's cover choice


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
    res = assistant.search_chat(
        [{"role": "user", "content": "factory in Senai"}], lambda f: seen.append(f) or [{"title": "A"}, {"title": "B"}])
    assert res.reply == "Two factories in Senai match."
    assert res.filters == SearchFilters(location="Senai") and len(res.results) == 2 and len(seen) == 1
    assert res.model_name == "fake:primary" and res.declined is None


def test_search_chat_falls_back_and_records_model(monkeypatch):
    assistant = fake_assistant(monkeypatch, BrokenModel(messages=iter([])),
                               ToolFakeModel(messages=search_replies("backup")))
    recorder = calllog.CallRecorder()
    res = assistant.search_chat([{"role": "user", "content": "factory in Senai"}], lambda f: [{"title": "A"}],
                                recorder=recorder)
    assert res.reply == "Two factories in Senai match." and len(res.results) == 1
    assert res.model_name == "fake:backup"
    # Every call is recorded. The main model is tried first on each of the two loop steps, fails,
    # and the backup answers.
    assert [c["ok"] for c in recorder.calls] == [False, True, False, True]
    assert recorder.calls[0]["error"] == "RuntimeError" and recorder.calls[1]["served_by"] == "backup"
    assert {c["step"] for c in recorder.calls} == {"search_chat"}


def test_redact_removes_photos_but_keeps_text():
    data = {"messages": [{"content": [{"type": "text", "text": "Factory in Senai"},
                                      {"type": "image", "base64": "A" * 5000, "mime_type": "image/jpeg"}]}],
            "images": [(b"\xff\xd8 jpeg bytes", "image/jpeg")], "url": "data:image/png;base64,AAAA"}
    out = redact(data)
    content = out["messages"][0]["content"]
    assert content[0]["text"] == "Factory in Senai" and content[1]["base64"] == PHOTO
    assert out["images"][0] == [PHOTO, "image/jpeg"] and out["url"] == PHOTO


def test_out_of_scope_request_gets_fixed_reply_without_search(monkeypatch):
    decline = AIMessage("", tool_calls=[{"name": "decline_request", "args": {"reason": "shop_or_office"}, "id": "1"}],
                        response_metadata={"model_name": "primary"})
    assistant = fake_assistant(monkeypatch, ToolFakeModel(messages=iter([decline])))  # one reply only
    recorder = calllog.CallRecorder()
    res = assistant.search_chat([{"role": "user", "content": "shop lot in Puchong"}],
                                lambda f: pytest.fail("must not search"), recorder=recorder)
    assert res.declined == "shop_or_office" and res.reply == llm.DECLINE_REPLIES["shop_or_office"]
    assert res.results is None and len(recorder.calls) == 1  # no second model call


def test_buyer_contact_details_are_masked_before_the_model(monkeypatch):
    seen = []

    class Spy(ToolFakeModel):
        def _generate(self, messages, *args, **kwargs):
            seen.extend(m.text for m in messages)
            return super()._generate(messages, *args, **kwargs)

    assistant = fake_assistant(monkeypatch, Spy(messages=search_replies("primary")))
    assistant.search_chat([{"role": "user", "content": "factory in Senai, call me 012-345 6789"}], lambda f: [])
    assert not any("012-345 6789" in t for t in seen) and any("[phone]" in t for t in seen)


def test_mask_contact_details_but_not_specs():
    assert pii.mask("Call 012-345 6789, +60 3-5555 1234 or ali@abc.com.my; IC 850101-14-5678") == (
        "Call [phone], [phone] or [email]; IC [IC number]")
    specs = "RM 4,500,000, 0.5 acre, 400A, 43560 sqft, 2 ekar, Lot 12345, Jalan 6/10, built 2024"
    assert pii.mask(specs) == specs


# Each prompt version pinned to its prompt text. If this fails, a prompt changed:
# bump PROMPT_VERSION or SEARCH_PROMPT_VERSION and record the new fingerprint here.
PROMPT_FINGERPRINTS = {"ads-v5-covers": "9d469925a4d8", "search-v2-scoped": "a3b23c6b519d", "search-v3-media": "f4e54a580c66"}


def test_prompt_edits_need_a_new_version():
    assert PROMPT_FINGERPRINTS[llm.PROMPT_VERSION] == llm.AD_PROMPT_FINGERPRINT
    assert PROMPT_FINGERPRINTS[llm.SEARCH_PROMPT_VERSION] == llm.SEARCH_PROMPT_FINGERPRINT


def test_invalid_structured_output_is_recorded_as_failure():
    recorder = calllog.CallRecorder()
    recorder.calls.append({"step": "check_drafts", "ok": True, "error": None})
    recorder.format_failure("check_drafts")
    assert recorder.calls[0] == {"step": "check_drafts", "ok": False, "error": "InvalidStructuredOutput"}
