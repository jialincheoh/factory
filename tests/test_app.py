"""End-to-end tests of the website with the offline stand-in model:
agent signs in, uploads photos, reviews drafts, publishes; buyer searches,
opens the listing and reveals the phone number; every step is logged."""
import io
import re
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, security
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
    # Factories and warehouses only: no land pages.
    assert client.get("/land-for-sale").status_code == 404

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
    assert "doesn't look like a factory or warehouse" in page
    cand_id = re.search(r'name="choice" value="([0-9a-f-]{36})"', page)[1]
    r = client.post(review_url.replace("/agent/requests/", "/agent/requests/") + "/publish", data={
        "csrf": token, "choice": cand_id, f"headline_{cand_id}": "h", f"body_{cand_id}": "b",
        "property_type": "shop_office", "deal_type": "rent"})
    assert r.status_code == 400 and "shoplot.com.my" in r.text
