"""Video tours: real ffmpeg renders, stored in the test database and served over HTTP."""
import io
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import imageio_ffmpeg
import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, images, security, video
from app.main import app

COLOURS = [(190, 60, 50), (50, 150, 70), (50, 80, 190), (210, 180, 60)]


def photo(colour, size=(800, 450)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def probe(data: bytes) -> str:
    """ffmpeg's description of the file; it exits non-zero without an output, so ignore that."""
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(data)
        f.flush()
        return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", f.name],
                              capture_output=True, text=True).stderr


def seconds_in(info: str) -> float:
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def frame_colour(data: bytes, at: float) -> tuple[int, int, int]:
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "v.mp4", Path(tmp) / "f.png"
        src.write_bytes(data)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-ss", str(at), "-i", str(src),
                        "-frames:v", "1", str(out)], check=True)
        return Image.open(out).convert("RGB").getpixel((480, 270))


def close_to(actual, expected, tolerance=30) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def drain():
    while video.render_next():
        pass


def video_row(listing_id):
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM listing_videos WHERE listing_id = %s", [listing_id]).fetchone()


@pytest.fixture(scope="module")
def agent():
    agent_id = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
                     "VALUES (%s, 'Video Agent', %s, %s, '+00 5555 0001')",
                     [agent_id, f"{agent_id}@example.com", security.hash_password("correct-horse-battery")])
    return agent_id


def make_listing(agent_id, photos: list[bytes], status="published", title="Video test factory",
                 video_on=True) -> uuid.UUID:
    listing_id = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type, "
                     "location, state, price_listed, cover_position, video_enabled) "
                     "VALUES (%s,%s,%s,%s,'factory','sale','Shah Alam','Selangor',1000000,1,%s)",
                     [listing_id, agent_id, status, title, video_on])
        conn.execute("INSERT INTO published_ads (ad_id, listing_id, channel, published_at, headline, ad_text) "
                     "VALUES (%s,%s,'own_portal',now(),'h','b')", [uuid.uuid4(), listing_id])
        for position, data in enumerate(photos, start=1):
            name = images.save(data, conn)
            conn.execute("INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                         "VALUES (%s,%s,%s,%s,800,450)", [uuid.uuid4(), listing_id, position, name])
    return listing_id


def page_of(client, listing_id) -> str:
    r = client.get(f"/listings/{listing_id}", follow_redirects=True)
    assert r.status_code == 200
    return r.text


def test_film_shows_each_photo_in_order():
    data = video.render([photo(c) for c in COLOURS[:3]])
    info = probe(data)
    assert "h264" in info and "960x540" in info and "Audio:" not in info
    assert seconds_in(info) == pytest.approx(video.duration_seconds(3), abs=0.2)
    step = video.SECONDS_PER_PHOTO - video.FADE_SECONDS
    for i, colour in enumerate(COLOURS[:3]):
        assert close_to(frame_colour(data, i * step + video.FADE_SECONDS + 0.5), colour), f"photo {i}"


def test_portrait_photo_is_not_cropped():
    tall = Image.new("RGB", (400, 800), (250, 250, 250))
    tall.paste((10, 10, 10), (0, 0, 400, 40))  # a band a crop to 16:9 would remove
    buf = io.BytesIO()
    tall.save(buf, "JPEG")
    canvas = video._canvas(buf.getvalue())
    assert canvas.size == (video.WIDTH * 2, video.HEIGHT * 2)
    centre_x = canvas.width // 2
    assert sum(canvas.getpixel((centre_x, 20))) < 120  # the top of the photo is still in the frame


def test_needs_two_photos():
    with pytest.raises(video.VideoError):
        video.render([photo(COLOURS[0])])


def test_published_listing_gets_a_playable_video(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    client = TestClient(app)
    assert "video-tour" not in page_of(client, listing_id)  # nothing until it is rendered

    drain()
    row = video_row(listing_id)
    assert row["status"] == "ready" and row["photo_count"] == 2 and row["finished_at"]
    html = page_of(client, listing_id)
    url = f"/videos/{row['file_name']}"
    assert f'src="{url}"' in html and "Video tour" in html and "camera movement" in html

    whole = client.get(url)
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4"
    assert whole.content == bytes(row["data"])
    assert "immutable" in whole.headers["cache-control"]
    assert whole.headers["accept-ranges"] == "bytes"

    # Safari asks for ranges and refuses to play a server that ignores them.
    part = client.get(url, headers={"Range": "bytes=0-1", "Accept-Encoding": "gzip"})
    assert part.status_code == 206 and part.content == whole.content[:2]
    assert part.headers["content-range"] == f"bytes 0-1/{len(whole.content)}"
    assert part.headers.get("content-encoding", "identity") == "identity"
    assert "immutable" in part.headers["cache-control"]


def test_video_survives_a_wiped_disk(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    drain()
    row = video_row(listing_id)
    client = TestClient(app)
    assert client.get(f"/videos/{row['file_name']}").status_code == 200
    (config.UPLOAD_DIR / row["file_name"]).unlink()
    for image in config.UPLOAD_DIR.glob("*.jpg"):  # the photos go too
        image.unlink()
    again = client.get(f"/videos/{row['file_name']}")
    assert again.status_code == 200 and again.content == bytes(row["data"])


def test_which_listings_get_videos(agent):
    single = make_listing(agent, [photo(COLOURS[0])])
    draft = make_listing(agent, [photo(c) for c in COLOURS[:2]], status="draft")
    sold = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO outcomes (listing_id, status, ended_at, sold_price) VALUES (%s,'sold',now(),1)", [sold])
    drain()
    assert video_row(single) is None and video_row(draft) is None and video_row(sold) is None


def test_hidden_when_listing_is_unpublished(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    drain()
    url = f"/videos/{video_row(listing_id)['file_name']}"
    client = TestClient(app)
    assert client.get(url).status_code == 200
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listings SET status = 'draft' WHERE listing_id = %s", [listing_id])
    assert client.get(url).status_code == 404


def test_bad_video_names_are_refused():
    client = TestClient(app)
    assert client.get("/videos/secret.txt").status_code == 404
    assert client.get("/videos/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get(f"/videos/{uuid.uuid4().hex}.mp4").status_code == 404


def test_new_cover_makes_a_new_video(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    drain()
    first = video_row(listing_id)
    assert close_to(frame_colour(bytes(first["data"]), 1.0), COLOURS[0])
    assert not video.render_next()  # up to date: nothing to do

    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listings SET cover_position = 2 WHERE listing_id = %s", [listing_id])
    drain()
    second = video_row(listing_id)
    assert second["file_name"] != first["file_name"] and second["photos_key"] != first["photos_key"]
    assert close_to(frame_colour(bytes(second["data"]), 1.0), COLOURS[1])


def test_failures_stop_after_the_limit_and_do_not_block_others(agent, monkeypatch):
    bad = make_listing(agent, [photo(c) for c in COLOURS[:2]], title="Bad photos")
    drain()
    good_row = video_row(bad)
    assert good_row["status"] == "ready"
    with psycopg.connect(config.DATABASE_URL) as conn:  # force a re-render that will fail
        conn.execute("UPDATE listing_videos SET photos_key = 'old' WHERE listing_id = %s", [bad])

    def boom(_photos):
        raise video.VideoError("disk full")

    monkeypatch.setattr(video, "render", boom)
    drain()  # returns: it must not retry for ever
    row = video_row(bad)
    assert row["status"] == "failed" and row["attempts"] == video.MAX_ATTEMPTS and "disk full" in row["error"]
    assert row["file_name"] == good_row["file_name"]  # the previous film stays up
    assert not video.render_next()
    monkeypatch.undo()

    other = make_listing(agent, [photo(c) for c in COLOURS[:2]], title="Good photos")
    drain()
    assert video_row(other)["status"] == "ready"


def test_a_render_in_progress_is_not_started_twice_but_a_dead_one_is_retried(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]])
    with psycopg.connect(config.DATABASE_URL, row_factory=psycopg.rows.dict_row) as conn:
        row = next(r for r in video.pending(conn) if r["listing_id"] == listing_id)
        conn.execute(video.CLAIM, {"id": listing_id, "key": video.photos_key(row["names"]),
                                   "lease": video.LEASE_MINUTES})
    assert not video.render_next()  # another worker holds the lease

    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("UPDATE listing_videos SET started_at = now() - interval '2 hours' WHERE listing_id = %s",
                     [listing_id])
    drain()
    row = video_row(listing_id)
    assert row["status"] == "ready" and row["attempts"] == 2


def signed_in(agent_id):
    client = TestClient(app)
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/agent/login").text)[1]
    r = client.post("/agent/login", data={"email": f"{agent_id}@example.com", "password": "correct-horse-battery",
                                          "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    return client, re.search(r'name="csrf" value="([^"]+)"', client.get("/agent/new").text)[1]


def toggle(client, token, listing_id):
    return client.post(f"/agent/listings/{listing_id}/video", data={"csrf": token}, follow_redirects=False)


def test_video_is_off_until_the_agent_turns_it_on(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]], title="Off by default", video_on=False)
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, status, title, property_type, deal_type) "
                     "VALUES (%s,%s,'draft','Column default','factory','sale')", [uuid.uuid4(), agent])
        assert conn.execute("SELECT count(*) FROM listings WHERE title = 'Column default' AND NOT video_enabled"
                            ).fetchone()[0] == 1
    drain()
    assert video_row(listing_id) is None
    visitor = TestClient(app)
    assert "video-tour" not in page_of(visitor, listing_id)

    client, token = signed_in(agent)
    dashboard = client.get("/agent").text
    assert f'action="/agent/listings/{listing_id}/video"' in dashboard and "Turn on" in dashboard
    assert toggle(client, token, listing_id).status_code == 303
    assert "Being made" in client.get("/agent").text

    drain()
    row = video_row(listing_id)
    assert row["status"] == "ready"
    assert "video-tour" in page_of(visitor, listing_id)
    assert visitor.get(f"/videos/{row['file_name']}").status_code == 200

    # Off hides it at once; back on shows the same film without another render.
    assert toggle(client, token, listing_id).status_code == 303
    assert "video-tour" not in page_of(visitor, listing_id)
    assert visitor.get(f"/videos/{row['file_name']}").status_code == 404
    assert not video.render_next()
    assert toggle(client, token, listing_id).status_code == 303
    assert not video.render_next()
    assert video_row(listing_id)["file_name"] == row["file_name"]
    assert "video-tour" in page_of(visitor, listing_id)


def test_only_the_listing_agent_can_switch_video(agent):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]], title="Not yours", video_on=False)
    other = uuid.uuid4()
    with psycopg.connect(config.DATABASE_URL) as conn:
        conn.execute("INSERT INTO real_estate_agents (real_estate_agent_id, display_name, email, password_hash, phone) "
                     "VALUES (%s, 'Other', %s, %s, '+00 5555 0003')",
                     [other, f"{other}@example.com", security.hash_password("correct-horse-battery")])
    client, token = signed_in(other)
    assert toggle(client, token, listing_id).status_code == 404
    assert client.post(f"/agent/listings/{listing_id}/video", data={}).status_code in (400, 403)
    assert TestClient(app).post(f"/agent/listings/{listing_id}/video", data={},
                                follow_redirects=False).status_code in (303, 401, 403)
    assert toggle(*signed_in(agent), listing_id).status_code == 303  # still off before this, now on
    with psycopg.connect(config.DATABASE_URL) as conn:
        assert conn.execute("SELECT video_enabled FROM listings WHERE listing_id = %s", [listing_id]).fetchone()[0]
    drain()


def test_one_photo_cannot_be_turned_on(agent):
    listing_id = make_listing(agent, [photo(COLOURS[0])], title="Single photo", video_on=False)
    client, token = signed_in(agent)
    assert toggle(client, token, listing_id).status_code == 400
    assert "Needs 2+ photos" in client.get("/agent").text


def test_turning_on_again_retries_a_failed_video(agent, monkeypatch):
    listing_id = make_listing(agent, [photo(c) for c in COLOURS[:2]], title="Retry me", video_on=False)
    client, token = signed_in(agent)
    toggle(client, token, listing_id)

    def boom(_photos):
        raise video.VideoError("disk full")

    monkeypatch.setattr(video, "render", boom)
    drain()
    assert video_row(listing_id)["status"] == "failed"
    assert "Could not be made" in client.get("/agent").text
    monkeypatch.undo()
    assert not video.render_next()  # attempts used up
    toggle(client, token, listing_id)
    toggle(client, token, listing_id)
    drain()
    assert video_row(listing_id)["status"] == "ready"

