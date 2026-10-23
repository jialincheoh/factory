"""End-to-end tests of the website with the offline stand-in model:
agent signs in, uploads photos, reviews drafts, publishes; buyer searches,
opens the listing and reveals the phone number; every step is logged."""
import io
import json
import re
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, security
from app.ai import llm
from app.main import app, edit_distance

PASSWORD = "correct-horse-battery"


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (640, 480), (120, 160, 200))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}  # GPSInfo
    buf = io.BytesIO(); img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture(scope="module")
def agent():
    agent_id = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
                     "VALUES (%s, 'Test Agent', %s, %s, '+00 5555 0000')",
                     [agent_id, f"{agent_id}@example.com", security.hash_password(PASSWORD)])
    return {"id": agent_id, "email": f"{agent_id}@example.com"}


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


def sign_in(client, agent):
    token = csrf_from(client.get("/agent/login").text)
    r = client.post("/agent/login", data={"email": agent["email"], "password": PASSWORD, "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agent"
    return csrf_from(client.get("/agent/new").text)  # signing in starts a new session and token


def db_one(sql, params=()):
    with psycopg.connect(config.DATABASE_URL) as conn:
        return conn.execute(sql, params).fetchone()


def test_agent_pages_require_sign_in():
    client = TestClient(app)
    r = client.get("/agent/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/agent/login"


def test_wrong_password_and_missing_csrf_rejected(agent):
    client = TestClient(app)
    token = csrf_from(client.get("/agent/login").text)
    r = client.post("/agent/login", data={"email": agent["email"], "password": "nope-nope-nope", "csrf": token})
    assert r.status_code == 400 and "incorrect" in r.text
    r = client.post("/agent/login", data={"email": agent["email"], "password": PASSWORD})
    assert r.status_code == 403


def test_rejects_non_image_upload(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Factory for sale in Senai with 400A power.", "csrf": token},
                    files=[("photos", ("evil.jpg", b"<script>alert(1)</script>", "image/jpeg"))])
    assert r.status_code == 400 and "not a readable image" in r.text


def test_full_flow(agent):
    client = TestClient(app)
    token = sign_in(client, agent)

    # 1. Upload photos + short description (Malay and English mixed).
    desc = ("Kilang dijual di Shah Alam. 2 ekar, 35,000 sqft built-up, 400A power, 30 ft ceiling height, "
            "pegangan bebas, container access. Harga RM 18 juta.")
    r = client.post("/agent/new", data={"description": desc, "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg")),
                           ("photos", ("b.jpg", jpeg_with_gps(), "image/jpeg"))],
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    review_url = r.headers["location"]
    request_id = review_url.rsplit("/", 1)[1]

    # Stored photos are re-encoded with no EXIF (GPS stripped).
    name = db_one("SELECT file_name FROM listing_images i JOIN generation_requests g USING (listing_id) "
                  "WHERE g.request_id = %s ORDER BY position LIMIT 1", [request_id])[0]
    stored = Image.open(config.UPLOAD_DIR / name)
    assert not stored.getexif()

    # Every draft logged with a display position and the shuffle seed; card parsed.
    req = db_one("SELECT order_seed, experiment_arm, analysis FROM generation_requests WHERE request_id = %s", [request_id])
    card = req[2]["card"]
    assert req[1] == "random_order_v1"
    assert (card["property_type"], card["location"], card["land_area_acres"], card["power_amps"], card["tenure"],
            card["container_access"], card["price"]) == ("factory", "Shah Alam", 2.0, 400, "freehold", True, 18_000_000)
    n = db_one("SELECT count(*) FROM candidates WHERE request_id = %s", [request_id])[0]
    assert n == config.DRAFTS_PER_REQUEST

    # Unpublished photos are private.
    assert TestClient(app).get(f"/uploads/{name}").status_code == 404
    assert client.get(f"/uploads/{name}").status_code == 200

    # The disk is only a cache: a restart that wipes it loses no photos.
    original = (config.UPLOAD_DIR / name).read_bytes()
    (config.UPLOAD_DIR / name).unlink()
    r = client.get(f"/uploads/{name}")
    assert r.status_code == 200 and r.content == original and (config.UPLOAD_DIR / name).exists()

    # 2. Review page shows the filled card, and a map pin at the town centre.
    page = client.get(review_url).text
    assert 'name="power_amps" inputmode="numeric" value="400"' in page
    assert 'id="pin-map" data-lat="3.0733"' in page
    cand_id = re.search(r'name="choice" value="([0-9a-f-]{36})"', page)[1]
    headline = re.search(rf'name="headline_{cand_id}" value="([^"]*)"', page)[1]

    # 3. Publish with a small edit and an exact pin.
    form = {"csrf": token, "choice": cand_id, f"headline_{cand_id}": headline + "!",
            f"body_{cand_id}": "Edited body text.", "title": "Freehold factory with 400A in Shah Alam",
            "property_type": "factory", "deal_type": "sale", "location": "Shah Alam", "state": "Selangor",
            "price": "18,000,000", "land_area_acres": "2", "size_sqft": "35,000", "ceiling_height_ft": "30",
            "power_amps": "400", "power_kva": "", "tenure": "freehold", "lease_years_left": "99",
            "zoning": "medium_industrial", "container_access": "yes", "loading_bays": "3",
            "features": "Office, Guardhouse", "latitude": "3.081", "longitude": "101.52"}
    r = client.post(f"/agent/requests/{request_id}/publish", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    listing_url = r.headers["location"]
    assert listing_url.endswith("/freehold-factory-with-400a-in-shah-alam")  # readable URL
    listing_id = re.search(r"/listings/([0-9a-f-]{36})/", listing_url)[1]
    d = db_one("SELECT decision_type, kept_candidate_id, edit_distance FROM decisions WHERE request_id = %s", [request_id])
    assert d[0] == "kept" and str(d[1]) == cand_id and d[2] > 0
    row = db_one("SELECT tenure, lease_years_left, container_access, latitude, features FROM listings "
                 "WHERE listing_id = %s", [listing_id])
    assert row == ("freehold", None, True, 3.081, ["guardhouse", "office"])  # lease years dropped for freehold
    # A second decision on the same request is refused.
    assert client.post(f"/agent/requests/{request_id}/publish", data=form).status_code == 409

    # 4. Buyer: the query iProperty's filters cannot express.
    buyer = TestClient(app)
    r = buyer.post("/api/chat", json={"messages": [
        {"role": "user", "content": "Factory in Shah Alam with 400 amp power and container access"}]})
    data = r.json()
    assert r.status_code == 200 and "Freehold factory with 400A in Shah Alam" in data["html"]
    assert data["form"]["min_power"] == 400 and data["form"]["container"] is True and data["form"]["area"] == "Shah Alam"
    assert any(m["id"] == listing_id for m in data["markers"])
    # Follow-up keeps earlier requirements and adds one that excludes it.
    r = buyer.post("/api/chat", json={"messages": [
        {"role": "user", "content": "Factory in Shah Alam with 400 amp power and container access"},
        {"role": "assistant", "content": data["reply"]},
        {"role": "user", "content": "leasehold only"}]})
    assert "Freehold factory with 400A in Shah Alam" not in r.json()["html"]

    # 5. Listing page shows the specs and map; phone reveal is logged.
    page = buyer.get(listing_url).text
    assert "Edited body text." in page and "400 A, three-phase" in page and 'id="listing-map"' in page
    assert buyer.post(f"/api/listings/{listing_id}/phone").status_code == 403  # needs the fetch header
    r = buyer.post(f"/api/listings/{listing_id}/phone", headers={"X-Requested-With": "fetch"})
    assert r.json()["phone"] == "+00 5555 0000"
    counts = dict(psycopg.connect(config.DATABASE_URL).execute(
        "SELECT event_type, count(*) FROM ad_events e JOIN published_ads p USING (ad_id) "
        "WHERE p.listing_id = %s GROUP BY 1", [listing_id]).fetchall())
    assert counts["impression"] >= 1 and counts["click"] == 1 and counts["phone_reveal"] == 1


@pytest.fixture(scope="module")
def warehouses(agent):
    """Two published listings: power quoted in kVA (1,000 kVA = about 1,391 A) and in amps."""
    ids = {}
    with psycopg.connect(config.DATABASE_URL) as conn:
        for key, kva, amps, deal, container in (("kva", 1000, None, "rent", True), ("amps", None, 150, "rent", False)):
            lid = uuid.uuid4()
            conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                         "location, state, power_kva, power_amps, container_access, price_listed) "
                         "VALUES (%s,%s,'published',%s,'warehouse',%s,'Port Klang','Selangor',%s,%s,%s,50000)",
                         [lid, agent["id"], f"Test warehouse {key}", deal, kva, amps, container])
            conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                         "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
            ids[key] = lid
    return ids


def test_kva_listing_matches_amp_filter(warehouses):
    r = TestClient(app).post("/api/search", json={"type": "warehouse", "deal": "rent", "min_power": "1000"})
    html = r.json()["html"]
    assert "Test warehouse kva" in html and "Test warehouse amps" not in html


def test_filter_bar_works_without_javascript(warehouses):
    page = TestClient(app).get("/?type=warehouse&area=Port+Klang&container=1").text
    assert "Test warehouse kva" in page and "Test warehouse amps" not in page
    assert 'name="container" value="1" checked' in page


def test_chat_starts_from_filter_bar(warehouses):
    r = TestClient(app).post("/api/chat", json={
        "messages": [{"role": "user", "content": "warehouses in Port Klang"}],
        "filters": {"container": "1"}})
    html = r.json()["html"]
    assert "Test warehouse kva" in html and "Test warehouse amps" not in html


def test_bad_filter_values_are_ignored():
    assert TestClient(app).get("/?min_power=abc&state=Atlantis&type=castle").status_code == 200


def test_creativity_ranking(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Factory for sale in Senai, 1 acre, 200A power.", "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg"))], follow_redirects=False)
    request_id = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(r.headers["location"]).text
    ids = re.findall(r'name="rank_([0-9a-f-]{36})"', page)
    assert len(ids) == config.DRAFTS_PER_REQUEST
    form = {"csrf": token, "choice": ids[0], f"headline_{ids[0]}": "Factory in Senai",
            f"body_{ids[0]}": "One acre, 200A.", "property_type": "factory", "deal_type": "sale"}
    url = f"/agent/requests/{request_id}/publish"

    # Partial or tied rankings are refused and nothing is saved.
    assert client.post(url, data={**form, f"rank_{ids[0]}": "1"}).status_code == 400
    assert client.post(url, data={**form, **{f"rank_{c}": "1" for c in ids}}).status_code == 400
    assert db_one("SELECT count(*) FROM decisions WHERE request_id = %s", [request_id])[0] == 0

    # A full ranking is stored with the decision.
    ranks = {f"rank_{c}": str(len(ids) - i) for i, c in enumerate(ids)}
    assert client.post(url, data={**form, **ranks}, follow_redirects=False).status_code == 303
    with psycopg.connect(config.DATABASE_URL) as conn:
        stored = dict(conn.execute("SELECT candidate_id::text, rank FROM rankings WHERE request_id = %s",
                                   [request_id]).fetchall())
    assert stored == {c: len(ids) - i for i, c in enumerate(ids)}


def test_regenerate_logs_kept_none(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Warehouse for rent in Klang, 20,000 sqft.", "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg"))], follow_redirects=False)
    first = r.headers["location"].rsplit("/", 1)[1]
    r = client.post(f"/agent/requests/{first}/regenerate", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and first not in r.headers["location"]
    assert db_one("SELECT decision_type FROM decisions WHERE request_id = %s", [first])[0] == "kept_none"


def test_chat_rejects_form_posts():
    assert TestClient(app).post("/api/chat", data={"messages": "x"}).status_code == 415


def test_security_headers():
    r = TestClient(app).get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"  # map tiles need an origin


def test_edit_distance():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("", "abc") == 3


def test_seo_pages(warehouses):
    client = TestClient(app)
    lid = warehouses["kva"]

    # Short and mistyped listing URLs redirect permanently to the one canonical URL.
    r = client.get(f"/listings/{lid}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/listings/{lid}/test-warehouse-kva"
    assert client.get(f"/listings/{lid}/wrong-slug", follow_redirects=False).status_code == 301

    page = client.get(f"/listings/{lid}/test-warehouse-kva").text
    assert '<link rel="canonical" href="http://localhost:8000/listings/' in page
    assert '"@type": "RealEstateListing"' in page and '"priceCurrency": "MYR"' in page
    assert '"@type": "BreadcrumbList"' in page and 'content="index, follow"' in page

    # Landing page for a real search phrase, with a data-driven intro and breadcrumbs.
    page = client.get("/warehouse-for-rent/port-klang").text
    assert "<h1>Warehouses for rent in Port Klang</h1>" in page
    assert "2 listings available now" in page and "Gudang untuk disewa di Port Klang" in page
    assert 'href="/warehouse-for-rent/selangor"' in page  # breadcrumb up to the state page
    assert client.get("/warehouse-for-rent/atlantis").status_code == 404
    assert client.get("/castles-for-sale").status_code == 404
    # Empty landing pages are kept out of the index.
    assert 'content="noindex, follow"' in client.get("/warehouse-for-sale/perlis").text
    # Industrial and commercial land has its own pages.
    assert client.get("/land-for-sale").status_code == 200
    assert client.get("/commercial-building-for-sale").status_code == 200

    # Filtered views and agent pages are not indexed.
    assert 'content="noindex, follow"' in client.get("/?min_power=100").text
    assert client.get("/agent/login").headers["x-robots-tag"] == "noindex, nofollow"

    sitemap = client.get("/sitemap.xml").text
    assert f"/listings/{lid}/test-warehouse-kva</loc>" in sitemap and "/warehouse-for-rent/port-klang</loc>" in sitemap
    robots = client.get("/robots.txt").text
    assert "Disallow: /agent" in robots and "Sitemap: http://localhost:8000/sitemap.xml" in robots


def test_ended_listings_are_gone_and_icons_served(agent):
    client = TestClient(app)
    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','Sold factory','factory','sale',"
                     "'Senai','Johor',900000)", [lid, agent["id"]])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
    assert client.get(f"/listings/{lid}/sold-factory").status_code == 200
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO outcomes (listing_id, status, ended_at, sold_price) VALUES (%s,'sold',now(),850000)", [lid])
    r = client.get(f"/listings/{lid}/sold-factory")
    assert r.status_code == 410 and "no longer available" in r.text
    assert f"/listings/{lid}/" not in client.get("/sitemap.xml").text
    assert client.get("/listings/" + str(uuid.uuid4())).status_code == 404
    assert client.get("/favicon.ico").status_code == 200 and client.get("/static/icon.svg").status_code == 200
    assert '"@type": "Organization"' in client.get("/").text



def test_only_factories_and_warehouses_are_listed(agent):
    client = TestClient(app)
    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:  # e.g. an old shoplot row left in the database
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','Old shop office','shop_office','rent',"
                     "'Klang','Selangor',3000)", [lid, agent["id"]])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
    assert "Old shop office" not in client.get("/").text
    assert client.get(f"/listings/{lid}/old-shop-office").status_code == 404

    # An agent cannot publish anything else as a listing here.
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Shop office for rent in Klang, 1,500 sqft.", "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg"))], follow_redirects=False)
    review_url = r.headers["location"]
    page = client.get(review_url).text
    assert "doesn't look like a factory, warehouse, industrial land or a whole commercial building" in page
    cand_id = re.search(r'name="choice" value="([0-9a-f-]{36})"', page)[1]
    r = client.post(review_url.replace("/agent/requests/", "/agent/requests/") + "/publish", data={
        "csrf": token, "choice": cand_id, f"headline_{cand_id}": "h", f"body_{cand_id}": "b",
        "property_type": "shop_office", "deal_type": "rent"})
    assert r.status_code == 400 and "kedai.my" in r.text


def test_privacy_notice():
    client = TestClient(app)
    page = client.get("/privacy").text
    assert "Personal Data Protection Act 2010" in page and 'id="bm"' in page and "Akta Perlindungan Data Peribadi" in page
    assert "OpenStreetMap" in page and "which draft you keep" in page and "Google" in page
    assert "We store each search: the words you typed" in page and "Kami menyimpan setiap carian" in page
    assert "We do not store your search text" not in page and "after 8 hours" in page
    assert ("Before launch" in page) == (not (config.OPERATOR_NAME and config.PRIVACY_EMAIL))
    assert 'href="/privacy"' in client.get("/").text and 'href="/privacy"' in client.get("/agent/login").text



def test_searches_are_logged_with_an_anonymous_visitor_id():
    client = TestClient(app)
    client.post("/api/search", json={"deal": "rent"})
    client.post("/api/chat", json={"messages": [{"role": "user", "content": "freehold only please"}]})
    with psycopg.connect(config.DATABASE_URL) as conn:
        rows = conn.execute("SELECT visitor_id, method, query_text, filters, result_count, interpreter FROM search_events "
                            "ORDER BY search_id DESC LIMIT 2").fetchall()
        (vid_t, m_t, q_t, f_t, n_t, i_t), (vid_f, m_f, q_f, f_f, n_f, _) = rows
        assert (m_f, q_f, f_f["deal_type"]) == ("filters", None, "rent") and n_f is not None
        assert (m_t, q_t, i_t) == ("text", "freehold only please", "offline-rules") and f_t["tenure"] == "freehold"
        assert vid_t == vid_f  # same visit, same anonymous ID
        n = conn.execute("SELECT count(*) FROM ad_events WHERE visitor_id = %s", [vid_t]).fetchone()[0]
    assert n >= 0


def test_model_calls_logged_and_contact_details_masked(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    desc = "Gudang untuk disewa di Senai, 20,000 sqft, 200A. Call Ali 012-345 6789 or ali@example.com."
    r = client.post("/agent/new", data={"description": desc, "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg"))], follow_redirects=False)
    request_id = r.headers["location"].rsplit("/", 1)[1]
    prompt = db_one("SELECT prompt_text FROM generation_requests WHERE request_id = %s", [request_id])[0]
    assert "012-345 6789" not in prompt and "[phone]" in prompt and "[email]" in prompt
    drafts = db_one("SELECT string_agg(ad_text, ' ') FROM candidates WHERE request_id = %s", [request_id])[0]
    assert "012-345 6789" not in drafts
    call = db_one("SELECT job, step, model, ok, is_fallback, prompt_version, prompt_hash, settings FROM model_calls "
                  "WHERE request_id = %s", [request_id])
    assert call[:6] == ("write_ads", "analyse", "offline-rules", True, False, llm.PROMPT_VERSION) and call[6]

    buyer = TestClient(app)
    buyer.post("/api/chat", json={"messages": [{"role": "user", "content": "warehouse in Senai, my number 019-876 5432"}]})
    q, sid = db_one("SELECT query_text, search_id FROM search_events ORDER BY search_id DESC LIMIT 1")
    assert q == "warehouse in Senai, my number [phone]"
    assert db_one("SELECT job, prompt_version FROM model_calls WHERE search_id = %s", [sid]) == (
        "search_chat", llm.SEARCH_PROMPT_VERSION)


def test_out_of_scope_search_is_declined():
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "3 bedroom condo in Bangsar"}]})
    assert r.status_code == 200 and "no homes here" in r.json()["reply"] and "count" not in r.json()
    assert db_one("SELECT declined, result_count FROM search_events ORDER BY search_id DESC LIMIT 1") == (
        "residential", None)
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "factory with office in Senai"}]})
    assert "count" in r.json()  # an office inside a factory is in scope


def test_monitor_report_runs():
    from psycopg.rows import dict_row
    from scripts import monitor
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        alerts = monitor.report(conn, 7)
        assert isinstance(alerts, list)
        # A prompt edited without a new version is caught.
        conn.execute("INSERT INTO model_calls (job, step, model, is_fallback, ok, prompt_version, prompt_hash, settings) "
                     "VALUES ('search_chat', 'search_chat', 'x:y', false, true, %s, 'edited', '{}')", [llm.SEARCH_PROMPT_VERSION])
        assert any("different prompts" in a for a in monitor.report(conn, 7))
        conn.rollback()


def upload_three_photos(client, token, desc):
    colours = [(200, 60, 60), (60, 200, 60), (60, 60, 200)]
    files = []
    for i, c in enumerate(colours):
        buf = io.BytesIO(); Image.new("RGB", (640, 480), c).save(buf, "JPEG")
        files.append(("photos", (f"{i}.jpg", buf.getvalue(), "image/jpeg")))
    r = client.post("/agent/new", data={"description": desc, "csrf": token}, files=files, follow_redirects=False)
    request_id = r.headers["location"].rsplit("/", 1)[1]
    return request_id, client.get(r.headers["location"]).text


def test_cover_photo_suggested_per_draft_and_chosen_by_agent(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    request_id, page = upload_three_photos(client, token, "Factory for sale in Senai, 2 acres, 400A power, freehold.")
    # Each draft suggests a cover; the offline stand-in leads each draft with a different photo.
    covers = db_one("SELECT array_agg(cover_position ORDER BY cover_position) FROM candidates WHERE request_id = %s",
                    [request_id])[0]
    assert covers == [1, 2, 3] and page.count("Suggested cover: photo") == 3 and 'name="cover"' in page
    ids = re.findall(r'name="rank_([0-9a-f-]{36})"', page)
    form = {"csrf": token, "choice": ids[0], f"headline_{ids[0]}": "Cover test factory",
            f"body_{ids[0]}": "Two acres, 400A.", "property_type": "factory", "deal_type": "sale"}
    url = f"/agent/requests/{request_id}/publish"
    assert client.post(url, data={**form, "cover": "9"}).status_code == 400  # no such photo

    assert client.post(url, data={**form, "cover": "3"}, follow_redirects=False).status_code == 303
    listing_id, cover = db_one("SELECT l.listing_id, l.cover_position FROM listings l JOIN generation_requests g "
                               "USING (listing_id) WHERE g.request_id = %s", [request_id])
    assert cover == 3 and db_one("SELECT cover_position FROM decisions WHERE request_id = %s", [request_id])[0] == 3
    third = db_one("SELECT file_name FROM listing_images WHERE listing_id = %s AND position = 3", [listing_id])[0]
    home = TestClient(app).get("/").text
    card = home[home.index("Cover test factory") - 1500:home.index("Cover test factory")]
    assert third in card  # search results lead with the chosen cover


def test_cover_defaults_to_the_kept_drafts_suggestion(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    request_id, page = upload_three_photos(client, token, "Warehouse for rent in Klang, 30,000 sqft, 32 ft ceiling.")
    ids = re.findall(r'name="rank_([0-9a-f-]{36})"', page)
    suggested = db_one("SELECT cover_position FROM candidates WHERE candidate_id = %s", [ids[1]])[0]
    form = {"csrf": token, "choice": ids[1], f"headline_{ids[1]}": "Klang warehouse",
            f"body_{ids[1]}": "30,000 sqft.", "property_type": "warehouse", "deal_type": "rent"}
    assert client.post(f"/agent/requests/{request_id}/publish", data=form, follow_redirects=False).status_code == 303
    assert db_one("SELECT cover_position FROM decisions WHERE request_id = %s", [request_id])[0] == suggested


def test_chat_is_rate_limited_per_visitor(monkeypatch):
    monkeypatch.setattr(security, "CHAT_PER_VISITOR", security.RateLimit(2, 600))
    client = TestClient(app)
    msg = {"messages": [{"role": "user", "content": "warehouse in Klang"}]}
    assert [client.post("/api/chat", json=msg).status_code for _ in range(3)] == [200, 200, 429]
    assert TestClient(app).post("/api/chat", json=msg).status_code == 200  # another visitor is unaffected


def test_ad_writing_is_rate_limited_per_agent(agent, monkeypatch):
    monkeypatch.setattr(security, "ADS_PER_AGENT", security.RateLimit(0, 3600))
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Factory for sale in Senai with 400A power.", "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg"))])
    assert r.status_code == 429 and "limit for new ads" in r.text


def brochure_pdf(text: str, pictures: int = 2) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 60), text, fontsize=11)
    for i in range(pictures):
        buf = io.BytesIO(); Image.new("RGB", (900, 600), (20 * i, 110, 180)).save(buf, "PNG")
        page.insert_image(pymupdf.Rect(40, 90 + 90 * i, 160, 170 + 90 * i), stream=buf.getvalue())
    return doc.tobytes()


def test_brochure_alone_is_enough(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    pdf = brochure_pdf("Kilang dijual di Senai. 1 ekar, 20,000 sqft, 300A power. Harga RM 6 juta. Call 012-345 6789")
    r = client.post("/agent/new", data={"description": "", "csrf": token},
                    files=[("brochure_pdf", ("brochure.pdf", pdf, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303, r.text
    request_id = r.headers["location"].rsplit("/", 1)[1]
    desc, n_photos = db_one(
        "SELECT l.description_original, (SELECT count(*) FROM listing_images i WHERE i.listing_id = l.listing_id) "
        "FROM listings l JOIN generation_requests g USING (listing_id) WHERE g.request_id = %s", [request_id])
    assert "Kilang dijual di Senai" in desc and n_photos == 2
    drafts = db_one("SELECT string_agg(headline || ' ' || ad_text, ' ') FROM candidates WHERE request_id = %s",
                    [request_id])[0]
    assert "012-345 6789" not in drafts  # masked before the model saw it


def test_brochure_text_joins_the_description_and_photos_fill_up(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    pdf = brochure_pdf("Brochure says: 2 loading bays and a guardhouse.", pictures=config.MAX_IMAGES)
    r = client.post("/agent/new", data={"description": "Warehouse for rent in Port Klang, 50,000 sqft.", "csrf": token},
                    files=[("photos", ("a.jpg", jpeg_with_gps(), "image/jpeg")),
                           ("brochure_pdf", ("brochure.pdf", pdf, "application/pdf"))], follow_redirects=False)
    assert r.status_code == 303, r.text
    request_id = r.headers["location"].rsplit("/", 1)[1]
    desc, n_photos = db_one(
        "SELECT l.description_original, (SELECT count(*) FROM listing_images i WHERE i.listing_id = l.listing_id) "
        "FROM listings l JOIN generation_requests g USING (listing_id) WHERE g.request_id = %s", [request_id])
    assert desc.startswith("Warehouse for rent in Port Klang") and "[From the brochure]" in desc
    assert n_photos == config.MAX_IMAGES  # the agent's photo first, then brochure pictures up to the cap


def test_brochure_problems_are_explained(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "", "csrf": token},
                    files=[("brochure_pdf", ("b.pdf", b"%PDF-1.4 nonsense", "application/pdf"))])
    assert r.status_code == 400 and "not a readable PDF" in r.text
    r = client.post("/agent/new", data={"description": "", "csrf": token},
                    files=[("brochure_pdf", ("b.pdf", brochure_pdf("Short", pictures=1), "application/pdf"))])
    assert r.status_code == 400 and "at least 20 characters" in r.text
    r = client.post("/agent/new", data={"description": "A perfectly fine description of a factory.", "csrf": token})
    assert r.status_code == 400 and "at least one photo" in r.text


def wav_bytes(seconds: float = 1.0) -> bytes:
    import struct
    n = int(16000 * seconds)
    return (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", n * 2) + b"\x00\x00" * n)


MEDIA_HEADERS = {"X-Requested-With": "fetch"}


def test_voice_search_is_transcribed_then_searched(monkeypatch):
    from app.ai import get_assistant
    heard = {}
    def fake_transcribe(self, audio, mime, recorder=None):
        heard["mime"], heard["bytes"] = mime, len(audio)
        return "gudang untuk sewa di Port Klang, call me at 012-345 6789"
    monkeypatch.setattr(type(get_assistant()), "transcribe", fake_transcribe)
    client = TestClient(app)
    r = client.post("/api/chat/media", headers=MEDIA_HEADERS,
                    data={"messages": '[{"role":"user","content":"[voice message]"}]', "filters": "{}"},
                    files=[("audio", ("voice.wav", wav_bytes(), "audio/wav"))])
    assert r.status_code == 200, r.text
    assert heard == {"mime": "audio/wav", "bytes": len(wav_bytes())}
    body = r.json()
    assert body["heard"] == "gudang untuk sewa di Port Klang, call me at [phone]"  # shown to the buyer, masked
    assert "count" in body and body["form"]["deal"] == "rent" and body["form"]["type"] == "warehouse"
    method, query = db_one("SELECT method, query_text FROM search_events ORDER BY search_id DESC LIMIT 1")
    assert method == "voice" and query == "gudang untuk sewa di Port Klang, call me at [phone]"


def test_voice_with_no_words_asks_to_type():
    r = TestClient(app).post("/api/chat/media", headers=MEDIA_HEADERS,
                             data={"messages": '[{"role":"user","content":"[voice message]"}]'},
                             files=[("audio", ("voice.wav", wav_bytes(), "audio/wav"))])
    assert r.status_code == 200 and r.json()["heard"] == "" and "type" in r.json()["reply"]  # offline stand-in cannot hear
    r = TestClient(app).post("/api/chat/media", headers=MEDIA_HEADERS,
                             data={"messages": '[{"role":"user","content":"x"}]'},
                             files=[("audio", ("voice.mid", b"MThd", "audio/midi"))])
    assert r.status_code == 400 and "format" in r.json()["detail"]


def test_photo_search_sends_the_photo_with_the_words(monkeypatch):
    from app.ai import get_assistant
    seen = {}
    real = type(get_assistant()).search_chat
    def spy(self, history, run_search, current=None, recorder=None, image=None):
        seen["image"] = image; seen["last"] = history[-1]["content"]
        return real(self, history, run_search, current, recorder, image=image)
    monkeypatch.setattr(type(get_assistant()), "search_chat", spy)
    client = TestClient(app)
    r = client.post("/api/chat/media", headers=MEDIA_HEADERS,
                    data={"messages": '[{"role":"user","content":"factory like this in Johor"}]'},
                    files=[("photo", ("p.jpg", jpeg_with_gps(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    assert seen["last"] == "[photo] factory like this in Johor" and seen["image"][1] == "image/jpeg"
    assert not Image.open(io.BytesIO(seen["image"][0])).getexif()  # GPS stripped before the model sees it
    assert "count" in r.json()
    method, query = db_one("SELECT method, query_text FROM search_events ORDER BY search_id DESC LIMIT 1")
    assert method == "image" and query == "[photo] factory like this in Johor"


def test_media_endpoint_refuses_plain_forms_and_junk():
    client = TestClient(app)
    r = client.post("/api/chat/media", data={"messages": "[]"}, files=[("photo", ("p.jpg", jpeg_with_gps(), "image/jpeg"))])
    assert r.status_code == 415  # no X-Requested-With: a cross-site HTML form
    r = client.post("/api/chat/media", headers=MEDIA_HEADERS, data={"messages": '[{"role":"user","content":"hi"}]'})
    assert r.status_code == 400 and "voice message or a photo" in r.json()["detail"]
    r = client.post("/api/chat/media", headers=MEDIA_HEADERS, data={"messages": '[{"role":"user","content":"hi"}]'},
                    files=[("photo", ("p.jpg", b"not an image", "image/jpeg"))])
    assert r.status_code == 400 and "readable image" in r.json()["detail"]


# ---------------------------------------------------------------- streaming, revisions, semantic search, evals

def test_new_listing_streams_progress_then_redirects(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    buf = io.BytesIO(); Image.new("RGB", (640, 480), (90, 90, 90)).save(buf, "JPEG")
    with client.stream("POST", "/agent/new", headers={"Accept": "application/x-ndjson"},
                       data={"description": "Factory for sale in Senai, 1 acre, 200A power.", "csrf": token},
                       files=[("photos", ("a.jpg", buf.getvalue(), "image/jpeg"))]) as r:
        assert r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(line) for line in r.iter_lines() if line.strip()]
    steps = [m["step"] for m in lines if "step" in m]
    assert steps[0].startswith("Masked contact details") and any("wrote 3 drafts" in s for s in steps)
    assert lines[-1]["done"] is True and lines[-1]["redirect"].startswith("/agent/requests/")
    assert client.get(lines[-1]["redirect"]).status_code == 200
    # Validation errors are not streamed: the page comes back as usual.
    r = client.post("/agent/new", headers={"Accept": "application/x-ndjson"},
                    data={"description": "short", "csrf": token})
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/html")


def test_agent_feedback_revises_one_draft_and_resumes_the_same_thread(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    request_id, page = upload_three_photos(
        client, token, "Factory for sale in Senai. Two acres. 400A power. Freehold title. Container access.")
    ids = re.findall(r'name="rank_([0-9a-f-]{36})"', page)
    target = ids[0]
    before = db_one("SELECT ad_text FROM candidates WHERE candidate_id = %s", [target])[0]
    url = f"/agent/requests/{request_id}/drafts/{target}/revise"
    assert client.post(url, data={"feedback": "x", "csrf": token}).status_code == 400  # too short
    r = client.post(url, data={"feedback": "Make it shorter please, call 012-345 6789", "csrf": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#draft-{target}")
    feedback, ad_text = db_one("SELECT feedback, ad_text FROM draft_revisions WHERE candidate_id = %s", [target])
    assert feedback == "Make it shorter please, call [phone]" and len(ad_text) < len(before)
    page = client.get(f"/agent/requests/{request_id}").text
    assert "Revised 1 time" in page and ad_text in page
    assert db_one("SELECT ad_text FROM candidates WHERE candidate_id = %s", [target])[0] == before  # original kept

    # Second feedback resumes the paused graph from its Postgres checkpoint: still one thread.
    client.post(url, data={"feedback": "even shorter", "csrf": token})
    assert db_one("SELECT count(DISTINCT thread_id) FROM checkpoints WHERE thread_id = %s",
                  [f"revise:{request_id}"])[0] == 1
    assert db_one("SELECT count(*) FROM draft_revisions WHERE candidate_id = %s", [target])[0] == 2

    # Publishing the revised text unchanged counts as kept with no edits.
    latest = db_one("SELECT headline, ad_text FROM draft_revisions WHERE candidate_id = %s "
                    "ORDER BY created_at DESC LIMIT 1", [target])
    r = client.post(f"/agent/requests/{request_id}/publish", follow_redirects=False,
                    data={"csrf": token, "choice": target, f"headline_{target}": latest[0],
                          f"body_{target}": latest[1], "property_type": "factory", "deal_type": "sale"})
    assert r.status_code == 303
    assert db_one("SELECT decision_type, edit_distance FROM decisions WHERE request_id = %s", [request_id]) == ("kept", 0)
    assert client.post(url, data={"feedback": "too late", "csrf": token}).status_code == 409


def test_semantic_query_ranks_by_meaning():
    from app import db as appdb, embeddings
    from app.ai.schemas import SearchFilters
    with appdb.connect() as conn:
        embeddings.embed_missing(conn)
        missing = conn.execute("SELECT count(*) AS n FROM published_ads pa JOIN listings l USING (listing_id) "
                               "WHERE l.status = 'published' AND pa.embedding IS NULL").fetchone()["n"]
        assert missing == 0  # publishing (and the backfill) embeds every live ad
        rows = conn.execute("""SELECT pa.ad_id, pa.headline, pa.ad_text, l.title, l.location, l.state, l.features
                               FROM published_ads pa JOIN listings l USING (listing_id) WHERE l.status = 'published'
                               ORDER BY pa.published_at LIMIT 1""").fetchall()
        assert rows, "an earlier test publishes a listing"
        # Fake embeddings are deterministic: the exact ad text is the nearest neighbour of itself.
        results = appdb.search_listings(conn, SearchFilters(semantic_query=embeddings.ad_text(rows[0])))
        assert results[0]["ad_id"] == rows[0]["ad_id"]


def test_eval_dataset_export_and_local_run():
    from app import db as appdb
    from scripts import eval_ads
    with appdb.connect() as conn:
        examples = eval_ads.export_examples(conn)
    assert examples and all(e["outputs"]["card"]["property_type"] in ("factory", "warehouse") for e in examples)
    assert all("012-345" not in e["inputs"]["description"] for e in examples)
    summary = eval_ads.run_local(examples[:2])
    assert set(summary) == {"card_accuracy", "drafts_supported", "format_ok", "closeness_to_kept", "no_contact_details"}
    assert summary["card_accuracy"] is not None and 0 <= summary["card_accuracy"] <= 1
    assert summary["no_contact_details"] == 1.0


def test_agent_profile_shows_name_and_registration_on_listings(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/profile", data={"csrf": token, "display_name": "Grace Lin", "phone": "017-000 0000",
                                            "registration_no": "PEA 9999"})
    assert r.status_code == 200 and "Saved." in r.text and 'value="PEA 9999"' in r.text
    assert client.post("/agent/profile", data={"csrf": token, "display_name": "", "phone": ""}).status_code == 400
    assert client.post("/agent/profile", data={"csrf": token, "display_name": "G", "phone": "call me"}).status_code == 400
    r = TestClient(app).post("/agent/profile", data={"display_name": "x"}, follow_redirects=False)
    assert r.status_code in (303, 401, 403)  # signed-out visitors cannot change a profile

    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','Land in Puchong','land','sale',"
                     "'Puchong','Selangor',1000000)", [lid, agent["id"]])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
    page = TestClient(app).get(f"/listings/{lid}/land-in-puchong").text
    assert "Listed by Grace Lin (PEA 9999)" in page
    r = TestClient(app).post(f"/api/listings/{lid}/phone", headers={"x-requested-with": "fetch"})
    assert r.json() == {"phone": "017-000 0000", "name": "Grace Lin"}


def test_listings_without_a_pin_get_their_towns_centre(agent):
    from app import geo
    from scripts import migrate
    assert geo.lookup("Bandar Kinrara") == geo.AREAS["Bandar Kinrara"]
    assert geo.lookup("Bandar Kinrara, Puchong") == geo.AREAS["Bandar Kinrara"]  # the more specific name wins
    assert geo.locate("Somewhere new", "Commercial land in Bandar Puteri Puchong") == geo.AREAS["Puchong"]
    assert geo.locate(None, "Land in Atlantis") is None

    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','Land in BK5','land','sale',"
                     "'Bandar Kinrara','Selangor',1000000)", [lid, agent["id"]])
    migrate.backfill_coordinates()
    with psycopg.connect(config.DATABASE_URL) as conn:
        lat, lng = conn.execute("SELECT latitude, longitude FROM listings WHERE listing_id = %s", [lid]).fetchone()
    assert (float(lat), float(lng)) == geo.AREAS["Bandar Kinrara"][1:]


def test_footer_has_no_copyright_line():
    page = TestClient(app).get("/privacy").text
    assert "©" not in page


def test_agent_features_a_listing_on_the_homepage(agent):
    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listings SET featured_at = NULL")
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','Featured land in Kinrara','land','sale',"
                     "'Bandar Kinrara','Selangor',2000000)", [lid, agent["id"]])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
    assert "Featured listings" not in TestClient(app).get("/").text

    client = TestClient(app)
    token = sign_in(client, agent)
    assert client.post(f"/agent/listings/{lid}/feature", data={"csrf": token}, follow_redirects=False).status_code == 303
    home = TestClient(app).get("/").text
    featured = home[home.index('id="featured-heading"'):home.index('class="latest-heading"')]
    assert "Featured land in Kinrara" in featured
    assert "Featured listings" not in TestClient(app).get("/?type=land").text  # filtered views skip the section
    assert "Unfeature" in client.get("/agent").text

    # Signed-out visitors and other agents cannot feature it.
    assert TestClient(app).post(f"/agent/listings/{lid}/feature", data={}, follow_redirects=False).status_code in (303, 401, 403)
    assert client.post(f"/agent/listings/{uuid.uuid4()}/feature", data={"csrf": token}).status_code == 404

    client.post(f"/agent/listings/{lid}/feature", data={"csrf": token})  # toggles back off
    assert "Featured listings" not in TestClient(app).get("/").text


def test_glenmarie_is_recognised_and_specific_places_win():
    from app import geo
    assert geo.lookup("Temasya Glenmarie, Shah Alam") == geo.AREAS["Glenmarie"]
    assert geo.lookup("Hicom Glenmarie") == geo.AREAS["Glenmarie"]
    assert geo.lookup("Novus Business Park") == geo.AREAS["Glenmarie"]
    assert geo.lookup("Shah Alam") == geo.AREAS["Shah Alam"]
    assert geo.lookup("Klang") == geo.AREAS["Klang"]          # the "kl" alias must not fire inside a word
    assert geo.lookup("Port Klang") == geo.AREAS["Port Klang"]
    assert geo.lookup("KL") == geo.AREAS["Kuala Lumpur"]
    assert TestClient(app).get("/factory-for-rent/glenmarie").status_code == 200


def test_area_pages_find_listings_by_location_or_title(agent):
    lid = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed) VALUES (%s,%s,'published','4-storey factory in Hicom Glenmarie',"
                     "'factory','rent','Shah Alam','Selangor',33000)", [lid, agent["id"]])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), lid])
    assert "4-storey factory in Hicom Glenmarie" in TestClient(app).get("/factory-for-rent/glenmarie").text
    sitemap = TestClient(app).get("/sitemap.xml").text
    assert "/factory-for-rent/glenmarie</loc>" in sitemap and "/factory-for-rent/shah-alam</loc>" in sitemap
