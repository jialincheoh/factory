"""Market check: what the Deep Agent reports only counts if its sources back it up."""

import re
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import config, security
from app.main import app
from app.services import market
from app.services.market import Comparable, MarketFindings, Sources

LISTING = {
    "listing_id": uuid.uuid4(),
    "title": "Semi-D factory",
    "property_type": "factory",
    "deal_type": "sale",
    "location": "Shah Alam",
    "state": "Selangor",
    "price": 4_000_000,
    "size_sqft": 10_000,
    "land_area_acres": 0.5,
    "tenure": "freehold",
}


def comparable(source_id="S1", price=3_000_000.0, built=10_000.0, **kw) -> Comparable:
    fields = dict(
        source_id=source_id,
        title="Factory",
        location="Shah Alam",
        deal_type="sale",
        property_type="factory",
        price_rm=price,
        built_area=built,
        built_unit="sqft",
        similarity="Same area",
    )
    return Comparable(**{**fields, **kw})


def sources_with(*texts) -> Sources:
    s = Sources()
    for i, text in enumerate(texts):
        s.add(f"Portal {i}", f"https://portal.example/{i}", [text])
    return s


def report(comps, sources, listing=LISTING):
    return market.build_report(listing, MarketFindings(comparables=comps), sources, 2)


def test_price_is_never_given_to_the_agent():
    text = market.brief(LISTING)
    assert "4000000" not in text and "4,000,000" not in text and "million" not in text
    assert "Shah Alam" in text and "10000 sqft" in text


@pytest.mark.parametrize(
    "value,text,expected",
    [
        (1_250_000, "Asking RM1,250,000 only", True),
        (1_250_000, "Asking RM1250000", True),
        (1_250_000, "priced at RM 1.25 million", True),
        (1_250_000, "RM1.25m negotiable", True),
        (1_250_000, "RM 1,350,000", False),
        (1.5, "Land: 1.5 acres", True),
        (1.5, "Land: 2.5 acres", False),
        (10_000, "Built-up 10,000 sq ft", True),
    ],
)
def test_numbers_must_appear_in_the_source(value, text, expected):
    assert market._appears(value, text) is expected


def test_numbers_are_worked_out_by_code():
    s = sources_with("RM3,000,000 for 10,000 sqft", "RM4,500,000 and 10,000 sqft", "RM6,000,000, 10,000 sqft")
    r = report([comparable("S1", 3e6), comparable("S2", 4.5e6), comparable("S3", 6e6)], s)
    assert r["enough"] and r["median"] == 450 and r["low"] == 300 and r["high"] == 600
    assert r["listing_per_sqft"] == 400 and r["position"] == "within"
    assert [c["per_sqft"] for c in r["comparables"]] == [300, 450, 600]


def test_invented_prices_and_sources_are_dropped():
    s = sources_with("RM3,000,000 for 10,000 sqft")
    r = report([comparable("S1", 9_999_000.0), comparable("S9", 3e6)], s)  # wrong price; a source no search returned
    assert r["comparables"] == [] and r["dropped"]["unverified"] == 2
    assert not r["enough"] and r["median"] is None and r["position"] is None


def test_too_few_comparables_give_no_verdict():
    s = sources_with("RM3,000,000 for 10,000 sqft", "RM5,000,000 for 10,000 sqft")
    r = report([comparable("S1", 3e6), comparable("S2", 5e6)], s)
    assert len(r["comparables"]) == 2 and not r["enough"] and r["position"] is None


def test_unlike_incomplete_and_same_property_are_left_out():
    s = sources_with("RM3,000,000 for 10,000 sqft", "RM4,000,000 for 10,000 sqft", "RM5,000 rent 10,000 sqft")
    r = report(
        [
            comparable("S3", 5000.0, deal_type="rent"),
            comparable("S1", 3e6, property_type="land"),
            comparable("S1", None),
            comparable("S2", 4e6),  # the same price and size as this listing: itself, on another portal
        ],
        s,
    )
    assert r["comparables"] == []
    assert r["dropped"] == {"unverified": 0, "not_similar": 2, "same_property": 1, "incomplete": 1}


def test_own_site_and_duplicates_do_not_count(monkeypatch):
    monkeypatch.setattr(config, "SITE_URL", "https://corpbiz.my")
    s = Sources()
    s.add("Ours", "https://corpbiz.my/listings/1", ["RM3,000,000 for 10,000 sqft"])
    s.add("Theirs", "https://portal.example/1", ["RM3,000,000 for 10,000 sqft"])
    r = report([comparable("S1", 3e6), comparable("S2", 3e6), comparable("S2", 3e6)], s)
    assert len(r["comparables"]) == 1 and r["comparables"][0]["url"] == "https://portal.example/1"


def test_acres_are_converted_and_land_compares_on_land():
    land = {**LISTING, "property_type": "land", "size_sqft": None, "price": 2_178_000, "land_area_acres": 1}
    s = sources_with("1 acre at RM2,000,000", "2 acres at RM4,000,000", "1.5 acres at RM3,000,000")
    comps = [
        comparable(f"S{i + 1}", p, built=None, land_area=a, land_unit="acre", property_type="land")
        for i, (a, p) in enumerate([(1, 2e6), (2, 4e6), (1.5, 3e6)])
    ]
    r = report(comps, s, land)
    assert r["basis"] == "land" and r["listing_per_sqft"] == pytest.approx(50)
    assert r["median"] == pytest.approx(2e6 / 43560)
    assert r["position"] == "above"


class ScriptedModel(GenericFakeChatModel):
    """Replays a list of replies; the real Deep Agent graph runs around it."""

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name, args, i):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}])


def test_deep_agent_uses_search_and_returns_the_report():
    final = {"comparables": [comparable("S1").model_dump()], "notes": "Little found."}
    model = ScriptedModel(
        messages=iter(
            [
                tool_call("web_search", {"query": "kilang Shah Alam"}, 1),
                tool_call("MarketFindings", final, 2),
            ]
        )
    )
    queries = []

    def fake_search(query):
        queries.append(query)
        return [{"title": "Portal", "url": "https://portal.example/1", "snippets": ["RM3,000,000 for 10,000 sqft"]}]

    findings, sources, used = market.investigate(LISTING, fake_search, model=model)
    assert queries == ["kilang Shah Alam"] and used == 1
    assert findings.notes == "Little found." and findings.comparables[0].source_id == "S1"
    assert sources.by_id["S1"]["url"] == "https://portal.example/1"


def test_search_limit_holds():
    calls = [tool_call("web_search", {"query": f"q{i}"}, i) for i in range(market.MAX_SEARCHES + 2)]
    model = ScriptedModel(messages=iter(calls + [tool_call("MarketFindings", {"comparables": []}, 99)]))
    seen = []
    _, _, used = market.investigate(LISTING, lambda q: seen.append(q) or [], model=model)
    assert used == market.MAX_SEARCHES and len(seen) == market.MAX_SEARCHES


# ---------------------------------------------------------------- the queue and the page


@pytest.fixture
def agent():
    agent_id = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
            "VALUES (%s, 'Market Agent', %s, %s, '+00 5555 0004')",
            [agent_id, f"{agent_id}@example.com", security.hash_password("correct-horse-battery")],
        )
    return agent_id


@pytest.fixture(autouse=True)
def only_new_listings():
    """Other tests' listings would otherwise be due too."""
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO outcomes (listing_id, status, ended_at) SELECT listing_id, 'withdrawn', now() "
            "FROM listings WHERE status = 'published' ON CONFLICT DO NOTHING"
        )


def make_listing(agent_id, price=4_000_000) -> uuid.UUID:
    listing_id = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
            "location, state, price_listed, size_sqft) VALUES (%s,%s,'published','Market test','factory','sale',"
            "'Shah Alam','Selangor',%s,10000)",
            [listing_id, agent_id, price],
        )
        conn.execute(
            "INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
            "VALUES (%s,%s,'own_portal',now(),'h','b')",
            [uuid.uuid4(), listing_id],
        )
    return listing_id


def signed_in(agent_id):
    client = TestClient(app)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/agent/login").text)[1]
    assert (
        client.post(
            "/agent/login",
            data={"email": f"{agent_id}@example.com", "password": "correct-horse-battery", "csrf": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    return client


def fake_investigate(listing, search, recorder):
    s = sources_with(
        "<script>x</script> RM3,000,000 for 10,000 sqft", "RM4,500,000 for 10,000 sqft", "RM6,000,000 for 10,000 sqft"
    )
    comps = [comparable("S1", 3e6, title="<script>alert(1)</script>"), comparable("S2", 4.5e6), comparable("S3", 6e6)]
    recorder.calls.append({"model": "test:model", "ok": True, "latency_ms": 5})
    return MarketFindings(comparables=comps, notes="Three found."), s, 3


def row(listing_id):
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM listing_market_checks WHERE listing_id = %s", [listing_id]).fetchone()


def test_every_published_listing_is_checked_and_only_its_agent_sees_it(agent, monkeypatch):
    monkeypatch.setattr(market, "investigate", fake_investigate)
    listing_id = make_listing(agent)
    url = f"/agent/listings/{listing_id}/market"
    client = signed_in(agent)
    assert "Not checked yet" in client.get("/agent").text
    assert "Not checked yet" in client.get(url).text

    assert market.check_next() == "done" and market.check_next() is None  # up to date: not repeated
    page = client.get(url).text
    assert "Not a valuation" in page and "within this range" in page and "RM 450" in page
    assert 'href="https://portal.example/0"' in page
    assert "<script>alert" not in page  # text from the web is escaped
    assert "View" in client.get("/agent").text

    # Buyers, signed-out visitors and other agents cannot see it, and buyers' pages do not mention it.
    assert TestClient(app).get(url, follow_redirects=False).status_code in (303, 401, 403)
    other = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
            "VALUES (%s,'Other',%s,%s,'+00 5555 0005')",
            [other, f"{other}@example.com", security.hash_password("correct-horse-battery")],
        )
    assert signed_in(other).get(url).status_code == 404
    public = TestClient(app).get(f"/listings/{listing_id}", follow_redirects=True).text
    assert "Market check" not in public and "portal.example" not in public

    with psycopg.connect(config.DATABASE_URL) as conn:
        assert conn.execute("SELECT count(*) FROM model_calls WHERE job = 'market_check'").fetchone()[0] >= 1


def test_price_change_triggers_a_new_check(agent, monkeypatch):
    monkeypatch.setattr(market, "investigate", fake_investigate)
    listing_id = make_listing(agent)
    assert market.check_next() == "done"
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listings SET price_listed = 7000000 WHERE listing_id = %s", [listing_id])
    assert market.check_next() == "done"
    assert row(listing_id)["report"]["position"] == "above"


def test_old_checks_are_refreshed(agent, monkeypatch):
    monkeypatch.setattr(market, "investigate", fake_investigate)
    listing_id = make_listing(agent)
    assert market.check_next() == "done"
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "UPDATE listing_market_checks SET finished_at = now() - interval '31 days' WHERE listing_id = %s",
            [listing_id],
        )
    assert market.check_next() == "done" and market.check_next() is None


def test_failure_is_recorded_and_waits_a_day_keeping_the_old_report(agent, monkeypatch):
    monkeypatch.setattr(market, "investigate", fake_investigate)
    listing_id = make_listing(agent)
    assert market.check_next() == "done"
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listings SET price_listed = 7000000 WHERE listing_id = %s", [listing_id])

    def quota(*_):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(market, "investigate", quota)
    assert market.check_next() == "failed"
    state = row(listing_id)
    assert state["status"] == "failed" and "429" in state["error"] and state["report"]["median"] == 450
    assert market.check_next() is None  # not retried straight away

    client = signed_in(agent)
    assert "similar listings" in client.get(f"/agent/listings/{listing_id}/market").text.lower()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "UPDATE listing_market_checks SET finished_at = now() - interval '25 hours' WHERE listing_id = %s",
            [listing_id],
        )
    monkeypatch.setattr(market, "investigate", fake_investigate)
    assert market.check_next() == "done"


def test_a_running_check_is_not_started_twice_but_a_dead_one_restarts(agent, monkeypatch):
    monkeypatch.setattr(market, "investigate", fake_investigate)
    listing_id = make_listing(agent)
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        assert market.next_listing(conn)["listing_id"] == listing_id
    assert market.check_next() is None
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute(
            "UPDATE listing_market_checks SET started_at = now() - interval '2 hours' WHERE listing_id = %s",
            [listing_id],
        )
    assert market.check_next() == "done"


def test_search_keeps_only_text_tied_to_a_page(monkeypatch):
    """Built from the SDK's own types, so a change in Google's response shape fails here."""
    from google import genai
    from google.genai import types

    reply = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text="ignored")]),
                grounding_metadata=types.GroundingMetadata(
                    grounding_chunks=[
                        types.GroundingChunk(
                            web=types.GroundingChunkWeb(title="portal-a.example", uri="https://portal-a.example/1")
                        ),
                        types.GroundingChunk(
                            web=types.GroundingChunkWeb(title="never cited", uri="https://portal-b.example/2")
                        ),
                    ],
                    grounding_supports=[
                        types.GroundingSupport(
                            segment=types.Segment(text="RM3,000,000 for 10,000 sqft"), grounding_chunk_indices=[0, 9]
                        )
                    ],
                ),
            )
        ]
    )
    sent = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            sent.update(kwargs)
            return reply

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(genai, "Client", FakeClient)
    assert market.gemini_search("kilang") == [
        {"title": "portal-a.example", "url": "https://portal-a.example/1", "snippets": ["RM3,000,000 for 10,000 sqft"]}
    ]
    assert sent["config"].tools[0].google_search is not None and "kilang" in sent["contents"]


def test_search_with_no_sources_returns_nothing(monkeypatch):
    from google import genai
    from google.genai import types

    class FakeModels:
        def generate_content(self, **kwargs):
            return types.GenerateContentResponse(candidates=[types.Candidate()])

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(genai, "Client", FakeClient)
    assert market.gemini_search("kilang") == []
