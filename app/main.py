"""The website: buyer-facing portal at /, real estate agent tools at /agent."""
import random
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import config, db, images, security
from .ai import get_assistant
from .ai.llm import PROMPT_VERSION
from .ai.schemas import PROPERTY_TYPES, SearchFilters

EXPERIMENT_ARM = "random_order_v1"  # drafts shown in seeded random order

app = FastAPI(title="Listing Ad Pipeline", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or secrets.token_hex(32),  # random = sessions reset on restart
    session_cookie="session",
    same_site="lax",
    https_only=config.SECURE_COOKIES,
    max_age=8 * 3600,
)
app.mount("/static", StaticFiles(directory=config.ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=config.ROOT / "app" / "templates")  # autoescapes HTML


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def money(value) -> str:
    if value is None:
        return "Price on request"
    return f"{config.CURRENCY}{float(value):,.0f}"


templates.env.filters["money"] = money


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.update(agent_id=security.current_agent_id(request), csrf=security.csrf_token(request),
               offline=config.LLM_MODEL == "offline")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def log_events(conn, ad_ids, event_type: str) -> None:
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ad_events (ad_id, event_type, occurred_at, source, source_event_id) "
            "VALUES (%s, %s, %s, 'portal', %s)",
            [(ad_id, event_type, now, uuid.uuid4().hex) for ad_id in ad_ids])


# ---------------------------------------------------------------- buyer side

@app.get("/", response_class=HTMLResponse)
def home(request: Request, conn=Depends(db.get_conn)):
    listings = db.search_listings(conn, SearchFilters(), limit=24)
    log_events(conn, [l["ad_id"] for l in listings], "impression")
    return render(request, "portal/home.html", listings=listings)


@app.post("/api/chat")
async def chat(request: Request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)  # JSON-only, so cross-site forms cannot post here
    body = await request.json()
    history = [
        {"role": "user" if m.get("role") == "user" else "assistant", "content": str(m.get("content", ""))[:1000]}
        for m in body.get("messages", [])[-12:] if isinstance(m, dict)
    ]
    if not history or history[-1]["role"] != "user" or not history[-1]["content"].strip():
        raise HTTPException(400, "Send a message.")

    def run():
        with db.connect() as conn:
            reply, filters, results = get_assistant().search_chat(history, lambda f: db.search_listings(conn, f))
            if results is not None:
                log_events(conn, [r["ad_id"] for r in results], "impression")
            return reply, filters, results

    try:
        reply, filters, results = await run_in_threadpool(run)
    except Exception:
        return JSONResponse({"reply": "Sorry, search is unavailable right now. Please try again."}, status_code=502)

    out = {"reply": reply}
    if results is not None:
        out["filters"] = filters.model_dump(exclude_none=True, exclude_defaults=True)
        out["count"] = len(results)
        out["html"] = templates.get_template("portal/_cards.html").render(listings=results, money=money)
    return out


@app.get("/listings/{listing_id}", response_class=HTMLResponse)
def listing_page(listing_id: uuid.UUID, request: Request, conn=Depends(db.get_conn)):
    rows = conn.execute(db.LISTING_CARD_SQL + " AND l.listing_id = %s", [listing_id]).fetchall()
    if not rows:
        raise HTTPException(404)
    listing = rows[0]
    photos = conn.execute("SELECT file_name FROM listing_images WHERE listing_id = %s ORDER BY position",
                          [listing_id]).fetchall()
    log_events(conn, [listing["ad_id"]], "click")
    return render(request, "portal/listing.html", listing=listing, photos=photos)


@app.post("/api/listings/{listing_id}/phone")
def reveal_phone(listing_id: uuid.UUID, request: Request, conn=Depends(db.get_conn)):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403)  # custom header forces a CORS preflight for cross-site callers
    row = conn.execute(
        "SELECT a.phone, a.display_name, pa.ad_id FROM listings l "
        "JOIN real_estate_agents a USING (real_estate_agent_id) "
        "JOIN LATERAL (SELECT ad_id FROM published_ads p WHERE p.listing_id = l.listing_id "
        "              ORDER BY published_at DESC LIMIT 1) pa ON true "
        "WHERE l.listing_id = %s AND l.status = 'published'", [listing_id]).fetchone()
    if not row:
        raise HTTPException(404)
    log_events(conn, [row["ad_id"]], "phone_reveal")
    conn.commit()
    return {"phone": row["phone"] or "Not provided", "name": row["display_name"]}


UPLOAD_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")


@app.get("/uploads/{name}")
def uploaded_photo(name: str, request: Request, conn=Depends(db.get_conn)):
    if not UPLOAD_NAME.match(name):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT l.status, l.real_estate_agent_id FROM listing_images i JOIN listings l USING (listing_id) "
        "WHERE i.file_name = %s", [name]).fetchone()
    # Photos of unpublished listings are visible only to the listing's agent.
    if not row or (row["status"] != "published"
                   and str(row["real_estate_agent_id"]) != security.current_agent_id(request)):
        raise HTTPException(404)
    return FileResponse(config.UPLOAD_DIR / name, media_type="image/jpeg")


# ------------------------------------------------------- real estate agent side

@app.get("/agent/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "agent/login.html")


@app.post("/agent/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          csrf: str = Form(None), conn=Depends(db.get_conn)):
    security.check_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    if not security.login_allowed(ip):
        return render(request, "agent/login.html", 429, error="Too many attempts. Try again in 15 minutes.")
    row = conn.execute("SELECT real_estate_agent_id, password_hash FROM real_estate_agents WHERE lower(email) = lower(%s)",
                       [email.strip()]).fetchone()
    if not security.verify_password(row["password_hash"] if row else None, password):
        security.record_failed_login(ip)
        return render(request, "agent/login.html", 400, error="Email or password is incorrect.", email=email)
    request.session.clear()  # new session on sign-in
    request.session["agent_id"] = str(row["real_estate_agent_id"])
    return RedirectResponse("/agent", status_code=303)


@app.post("/agent/logout")
def logout(request: Request, csrf: str = Form(None)):
    security.check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/agent", response_class=HTMLResponse)
def dashboard(request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)):
    agent = conn.execute("SELECT display_name FROM real_estate_agents WHERE real_estate_agent_id = %s",
                         [agent_id]).fetchone()
    listings = conn.execute("""
        SELECT l.listing_id, l.title, l.status, l.price_listed AS price, l.updated_at,
               (SELECT request_id FROM generation_requests g WHERE g.listing_id = l.listing_id
                 ORDER BY created_at DESC LIMIT 1) AS latest_request,
               count(e.*) FILTER (WHERE e.event_type = 'impression')   AS impressions,
               count(e.*) FILTER (WHERE e.event_type = 'click')        AS clicks,
               count(e.*) FILTER (WHERE e.event_type = 'phone_reveal') AS phone_reveals
        FROM listings l
        LEFT JOIN published_ads p ON p.listing_id = l.listing_id
        LEFT JOIN ad_events e ON e.ad_id = p.ad_id
        WHERE l.real_estate_agent_id = %s
        GROUP BY l.listing_id ORDER BY l.updated_at DESC""", [agent_id]).fetchall()
    return render(request, "agent/dashboard.html", agent=agent, listings=listings)


@app.get("/agent/new", response_class=HTMLResponse)
def new_listing_form(request: Request, agent_id: str = Depends(security.require_agent)):
    return render(request, "agent/new.html", max_images=config.MAX_IMAGES)


def _create_request(conn, agent_id, listing_id, description: str, photos: list[bytes]) -> uuid.UUID:
    """Call the model, then store the request and every draft in shuffled order."""
    assistant = get_assistant()
    started = time.monotonic()
    analysis, prompt_text = assistant.write_ads(description, [(p, "image/jpeg") for p in photos],
                                                config.DRAFTS_PER_REQUEST)
    latency_ms = int((time.monotonic() - started) * 1000)

    request_id, seed = uuid.uuid4(), random.SystemRandom().getrandbits(63)
    conn.execute(
        "INSERT INTO generation_requests (request_id, listing_id, real_estate_agent_id, model_name, "
        "prompt_version, prompt_text, experiment_arm, order_seed, analysis) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [request_id, listing_id, agent_id, assistant.model_name, PROMPT_VERSION, prompt_text,
         EXPERIMENT_ARM, seed, Jsonb(analysis.model_dump(exclude={"drafts"}))])

    order = list(range(len(analysis.drafts)))
    random.Random(seed).shuffle(order)  # reproducible from the logged seed
    raw = analysis.model_dump()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO candidates (candidate_id, request_id, display_position, headline, ad_text, "
            "raw_response, latency_ms) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(uuid.uuid4(), request_id, pos, analysis.drafts[i].headline, analysis.drafts[i].body,
              Jsonb({"generated_index": i, "analysis": raw}), latency_ms)
             for pos, i in enumerate(order, 1)])
    return request_id


@app.post("/agent/new")
async def create_listing(request: Request, agent_id: str = Depends(security.require_agent),
                         description: str = Form(...), photos: list[UploadFile] = File(...),
                         csrf: str = Form(None)):
    security.check_csrf(request, csrf)
    description = description.strip()
    error = None
    if not 20 <= len(description) <= 1500:
        error = "Write two or three sentences about the property (20 to 1,500 characters)."
    photos = [p for p in photos if p.filename]
    if not error and not 1 <= len(photos) <= config.MAX_IMAGES:
        error = f"Upload between 1 and {config.MAX_IMAGES} photos."
    processed = []
    if not error:
        try:
            for p in photos:
                processed.append(images.process_upload(await p.read(config.MAX_IMAGE_BYTES + 1)))
        except images.ImageError as exc:
            error = str(exc)
    if error:
        return render(request, "agent/new.html", 400, error=error, description=description,
                      max_images=config.MAX_IMAGES)

    def run():
        with db.connect() as conn:
            listing_id = uuid.uuid4()
            conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, description_original) "
                         "VALUES (%s, %s, %s)", [listing_id, agent_id, description])
            for pos, (data, w, h) in enumerate(processed, 1):
                conn.execute("INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                             "VALUES (%s,%s,%s,%s,%s,%s)", [uuid.uuid4(), listing_id, pos, images.save(data), w, h])
            return _create_request(conn, agent_id, listing_id, description, [d for d, _, _ in processed])

    try:
        request_id = await run_in_threadpool(run)
    except Exception as exc:
        return render(request, "agent/new.html", 502, description=description, max_images=config.MAX_IMAGES,
                      error=f"The ad writer failed ({type(exc).__name__}). Nothing was saved; please try again.")
    return RedirectResponse(f"/agent/requests/{request_id}", status_code=303)


def _load_request(conn, request_id, agent_id):
    req = conn.execute(
        "SELECT g.*, l.description_original, l.status AS listing_status FROM generation_requests g "
        "JOIN listings l USING (listing_id) WHERE g.request_id = %s AND g.real_estate_agent_id = %s",
        [request_id, agent_id]).fetchone()
    if not req:
        raise HTTPException(404)
    return req


@app.get("/agent/requests/{request_id}", response_class=HTMLResponse)
def review(request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent),
           conn=Depends(db.get_conn)):
    req = _load_request(conn, request_id, agent_id)
    drafts = conn.execute("SELECT candidate_id, display_position, headline, ad_text FROM candidates "
                          "WHERE request_id = %s ORDER BY display_position", [request_id]).fetchall()
    decision = conn.execute("SELECT * FROM decisions WHERE request_id = %s", [request_id]).fetchone()
    photos = conn.execute("SELECT file_name FROM listing_images WHERE listing_id = %s ORDER BY position",
                          [req["listing_id"]]).fetchall()
    request.session[f"opened:{request_id}"] = time.time()  # for seconds_to_decide
    return render(request, "agent/review.html", req=req, analysis=req["analysis"] or {}, drafts=drafts,
                  decision=decision, photos=photos, property_types=PROPERTY_TYPES)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance in characters."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _opt_int(value: str | None, field: str) -> int | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    if not value.isdigit():
        raise HTTPException(400, f"{field} must be a whole number.")
    return int(value)


@app.post("/agent/requests/{request_id}/publish")
async def publish(request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent),
                  conn=Depends(db.get_conn)):
    form = await request.form()
    security.check_csrf(request, form.get("csrf"))
    req = _load_request(conn, request_id, agent_id)
    if conn.execute("SELECT 1 FROM decisions WHERE request_id = %s", [request_id]).fetchone():
        raise HTTPException(409, "This request already has a decision.")

    choice = form.get("choice", "")
    if choice == "own":
        decision_type, candidate = "wrote_own", None
    else:
        try:
            candidate = conn.execute("SELECT * FROM candidates WHERE request_id = %s AND candidate_id = %s",
                                     [request_id, uuid.UUID(choice)]).fetchone()
        except ValueError:
            candidate = None
        if not candidate:
            raise HTTPException(400, "Pick one of the drafts or write your own.")
        decision_type = "kept"
    key = "own" if candidate is None else str(candidate["candidate_id"])
    headline = str(form.get(f"headline_{key}", "")).strip()[:120]
    body = str(form.get(f"body_{key}", "")).strip()[:4000]
    if not headline or not body:
        raise HTTPException(400, "The ad needs a headline and a body.")

    property_type = form.get("property_type")
    if property_type not in PROPERTY_TYPES:
        property_type = "other"
    price = (form.get("price") or "").strip().replace(",", "")
    try:
        price = float(price) if price else None
    except ValueError:
        raise HTTPException(400, "Price must be a number.")
    features = sorted({f.strip().lower() for f in str(form.get("features", "")).split(",") if f.strip()})[:30]

    bedrooms = _opt_int(form.get("bedrooms"), "Bedrooms")
    bathrooms = _opt_int(form.get("bathrooms"), "Bathrooms")
    size_sqft = _opt_int(form.get("size_sqft"), "Size")

    opened = request.session.pop(f"opened:{request_id}", None)
    final_text = f"{headline}\n\n{body}"
    conn.execute(
        "INSERT INTO decisions (request_id, decision_type, kept_candidate_id, final_text, edit_distance, "
        "decided_at, seconds_to_decide) VALUES (%s,%s,%s,%s,%s,now(),%s)",
        [request_id, decision_type, candidate and candidate["candidate_id"], final_text,
         edit_distance(f"{candidate['headline']}\n\n{candidate['ad_text']}", final_text) if candidate else None,
         int(time.time() - opened) if opened else None])
    conn.execute(
        "UPDATE listings SET status = 'published', title = %s, property_type = %s, location = %s, bedrooms = %s, "
        "bathrooms = %s, size_sqft = %s, price_listed = %s, features = %s, description_language = %s, "
        "updated_at = now() WHERE listing_id = %s",
        [str(form.get("title", "")).strip()[:120] or headline, property_type,
         str(form.get("location", "")).strip()[:120] or None,
         bedrooms, bathrooms, size_sqft, price, features,
         (req["analysis"] or {}).get("detected_language"), req["listing_id"]])
    conn.execute("INSERT INTO published_ads (ad_id, request_id, listing_id, channel, published_at, headline, ad_text) "
                 "VALUES (%s,%s,%s,'own_portal',now(),%s,%s)",
                 [uuid.uuid4(), request_id, req["listing_id"], headline, body])
    conn.commit()  # before redirecting, so the listing page sees it
    return RedirectResponse(f"/listings/{req['listing_id']}", status_code=303)


@app.post("/agent/requests/{request_id}/regenerate")
async def regenerate(request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent),
                     csrf: str = Form(None)):
    security.check_csrf(request, csrf)

    def run():
        with db.connect() as conn:
            req = _load_request(conn, request_id, agent_id)
            if conn.execute("SELECT 1 FROM decisions WHERE request_id = %s", [request_id]).fetchone():
                raise HTTPException(409, "This request already has a decision.")
            conn.execute("INSERT INTO decisions (request_id, decision_type, decided_at) VALUES (%s,'kept_none',now())",
                         [request_id])
            names = conn.execute("SELECT file_name FROM listing_images WHERE listing_id = %s ORDER BY position",
                                 [req["listing_id"]]).fetchall()
            photos = [(config.UPLOAD_DIR / n["file_name"]).read_bytes() for n in names]
            return _create_request(conn, agent_id, req["listing_id"], req["description_original"], photos)

    new_id = await run_in_threadpool(run)
    return RedirectResponse(f"/agent/requests/{new_id}", status_code=303)
