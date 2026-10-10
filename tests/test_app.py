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
    r = client.post("/agent/new", data={"description": "A lovely three bedroom house with a pool.", "csrf": token},
                    files=[("photos", ("evil.jpg", b"<script>alert(1)</script>", "image/jpeg"))])
    assert r.status_code == 400 and "not a readable image" in r.text


def test_full_flow(agent):
    client = TestClient(app)
    token = sign_in(client, agent)

    # 1. Upload photos + short description.
    desc = "Spacious 3 bedroom 2 bathroom bungalow in Maple Grove with a pool and garden. 2,000 sqft, asking $950k."
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

    # Every draft logged with a display position and the shuffle seed.
    req = db_one("SELECT order_seed, experiment_arm, analysis FROM generation_requests WHERE request_id = %s", [request_id])
    assert req[1] == "random_order_v1" and req[2]["card"]["bedrooms"] == 3
    n = db_one("SELECT count(*) FROM candidates WHERE request_id = %s", [request_id])[0]
    assert n == config.DRAFTS_PER_REQUEST

    # Unpublished photos are private.
    assert TestClient(app).get(f"/uploads/{name}").status_code == 404
    assert client.get(f"/uploads/{name}").status_code == 200

    # 2. Review page shows the filled card and the drafts.
    page = client.get(review_url).text
    assert 'name="bedrooms" inputmode="numeric" value="3"' in page
    assert "Maple Grove" in page
    cand_id = re.search(r'name="choice" value="([0-9a-f-]{36})"', page)[1]
    headline = re.search(rf'name="headline_{cand_id}" value="([^"]*)"', page)[1]

    # 3. Publish with a small edit.
    form = {"csrf": token, "choice": cand_id, f"headline_{cand_id}": headline + "!",
            f"body_{cand_id}": "Edited body text.", "title": "3-bedroom bungalow with pool", "property_type": "bungalow",
            "location": "Maple Grove", "bedrooms": "3", "bathrooms": "2", "size_sqft": "2,000", "price": "950000",
            "features": "Pool, garden"}
    r = client.post(f"/agent/requests/{request_id}/publish", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    listing_url = r.headers["location"]
    d = db_one("SELECT decision_type, kept_candidate_id, edit_distance FROM decisions WHERE request_id = %s", [request_id])
    assert d[0] == "kept" and str(d[1]) == cand_id and d[2] > 0
    # A second decision on the same request is refused.
    assert client.post(f"/agent/requests/{request_id}/publish", data=form).status_code == 409

    # 4. Buyer searches in plain language and finds it.
    buyer = TestClient(app)
    r = buyer.post("/api/chat", json={"messages": [{"role": "user", "content": "3-bedroom house with a pool"}]})
    assert r.status_code == 200 and r.json()["count"] >= 1
    assert "3-bedroom bungalow with pool" in r.json()["html"]
    # Follow-up keeps earlier requirements and adds a budget that excludes it.
    r = buyer.post("/api/chat", json={"messages": [
        {"role": "user", "content": "3-bedroom house with a pool"},
        {"role": "assistant", "content": r.json()["reply"]},
        {"role": "user", "content": "under 500k please"}]})
    assert "3-bedroom bungalow with pool" not in r.json()["html"]

    # 5. Listing page and phone reveal are logged against the ad.
    assert "Edited body text." in buyer.get(listing_url).text
    listing_id = listing_url.rsplit("/", 1)[1]
    assert buyer.post(f"/api/listings/{listing_id}/phone").status_code == 403  # needs the fetch header
    r = buyer.post(f"/api/listings/{listing_id}/phone", headers={"X-Requested-With": "fetch"})
    assert r.json()["phone"] == "+00 5555 0000"
    counts = dict(psycopg.connect(config.DATABASE_URL).execute(
        "SELECT event_type, count(*) FROM ad_events e JOIN published_ads p USING (ad_id) "
        "WHERE p.listing_id = %s GROUP BY 1", [listing_id]).fetchall())
    assert counts["impression"] >= 1 and counts["click"] == 1 and counts["phone_reveal"] == 1


def test_regenerate_logs_kept_none(agent):
    client = TestClient(app)
    token = sign_in(client, agent)
    r = client.post("/agent/new", data={"description": "Two bedroom condo with balcony and gym.", "csrf": token},
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


def test_edit_distance():
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("", "abc") == 3
