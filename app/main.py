"""The website: buyer-facing portal at /, real estate agent tools at /agent."""
import json
import random
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import brochure, config, db, embeddings, geo, images, revisions, security, seo
from .ai import get_assistant, pii
from .ai.calllog import CallRecorder, emit
from .ai.llm import AD_PROMPT_FINGERPRINT, PROMPT_VERSION, SEARCH_PROMPT_FINGERPRINT, SEARCH_PROMPT_VERSION
from .ai.schemas import PROPERTY_TYPES, ZONINGS, SearchFilters

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
app.add_middleware(GZipMiddleware, minimum_size=1000)
STATIC_DIR = config.ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Changes whenever a static file changes, so browsers can cache /static for a year.
STATIC_VERSION = str(int(max(f.stat().st_mtime for f in STATIC_DIR.rglob("*") if f.is_file())))
templates = Jinja2Templates(directory=config.ROOT / "app" / "templates")  # autoescapes HTML


# OpenStreetMap tiles are the only third-party resource; Leaflet itself is served from /static.
MAP_TILES = "https://tile.openstreetmap.org"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; img-src 'self' data: {MAP_TILES}; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    path = request.url.path
    if path.startswith("/static/") and request.query_params.get("v") == STATIC_VERSION:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/uploads/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # random, never-reused names
    if path.startswith(("/agent", "/api/")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    # OpenStreetMap refuses tiles without a Referer; send only our origin, never the page path.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def money(value) -> str:
    if value is None:
        return "Price on request"
    return f"{config.CURRENCY} {float(value):,.0f}"


def price_label(listing) -> str:
    if listing.get("price") is None:
        return "Price on request"
    return money(listing["price"]) + (" /mo" if listing.get("deal_type") == "rent" else "")


def type_label(value) -> str:
    return (value or "other").replace("_", " ").capitalize()


def number(value, digits: int = 0) -> str:
    return f"{float(value):,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{float(value):,.0f}"


templates.env.filters.update(money=money, price_label=price_label, type_label=type_label, number=number)
templates.env.globals.update(STATES=geo.STATES, AREAS=sorted(geo.AREAS), PROPERTY_TYPES=PROPERTY_TYPES,
                             ZONINGS=ZONINGS, MAP_TILES=MAP_TILES, currency=config.CURRENCY,
                             SITE_NAME=config.SITE_NAME,
                             now_year=lambda: datetime.now(timezone.utc).year,
                             listing_path=seo.listing_path, absolute=seo.absolute,
                             seo_script=seo.to_script,
                             static_version=STATIC_VERSION)


_footer_cache: dict = {"at": 0.0, "links": []}


def footer_links() -> list[tuple[str, str]]:
    """Links to landing pages that have listings, for crawlers and people. Cached for 5 minutes."""
    if time.time() - _footer_cache["at"] < 300:
        return _footer_cache["links"]
    links = []
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT property_type, deal_type, location, state, count(*) AS n FROM listings "
                                "WHERE status = 'published' AND listing_id NOT IN (SELECT listing_id FROM outcomes) "
                                "GROUP BY 1, 2, 3, 4 ORDER BY n DESC").fetchall()
        seen = set()
        for r in rows:
            path = seo.landing_path(r)
            if not path:
                continue
            heading = seo.LANDING[path][2]
            for place in (r["location"], r["state"]):
                if place and seo.place_from_slug(seo.slugify(place)) and (path, place) not in seen:
                    seen.add((path, place))
                    links.append((f"{heading} in {place}", f"/{path}/{seo.slugify(place)}"))
    except Exception:
        pass  # the footer must never break a page
    _footer_cache.update(at=time.time(), links=links[:40])
    return _footer_cache["links"]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("page", {})
    ctx.update(agent_id=security.current_agent_id(request), csrf=security.csrf_token(request),
               offline=config.LLM_MODEL == "offline", footer=footer_links())
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def visitor_id(request: Request) -> str:
    """Random ID kept in the session cookie for one visitor; never linked to an account or contact details."""
    vid = request.session.get("vid")
    if not vid:
        vid = request.session["vid"] = str(uuid.uuid4())
    return vid


def log_events(conn, ad_ids, event_type: str, vid: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ad_events (ad_id, event_type, occurred_at, source, source_event_id, visitor_id) "
            "VALUES (%s, %s, %s, 'portal', %s, %s)",
            [(ad_id, event_type, now, uuid.uuid4().hex, vid) for ad_id in ad_ids])


def log_search(conn, vid: str, method: str, filters: SearchFilters, result_count: int | None,
               query: str | None = None, interpreter: str | None = None, declined: str | None = None) -> int | None:
    """One row per search: how it was made, what was typed (contact details masked), the filters it
    became, and the result count. Returns the search_id, or None when search logging is off."""
    if not config.SEARCH_LOGGING:
        return None
    row = conn.execute(
        "INSERT INTO search_events (visitor_id, method, query_text, filters, result_count, interpreter, declined) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING search_id",
        [vid, method, pii.mask(query)[:1000] if query else None,
         Jsonb(filters.model_dump(exclude_none=True, exclude_defaults=True)), result_count, interpreter,
         declined]).fetchone()
    return row["search_id"]


def log_model_calls(conn, job: str, recorder: CallRecorder, request_id=None, search_id=None) -> None:
    """One model_calls row per model call, for monitoring (see scripts/monitor.py)."""
    assistant = get_assistant()
    version, fingerprint = ((PROMPT_VERSION, AD_PROMPT_FINGERPRINT) if job == "write_ads"
                            else (SEARCH_PROMPT_VERSION, SEARCH_PROMPT_FINGERPRINT))
    emit(job, recorder.calls)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO model_calls (job, request_id, search_id, step, model, served_by, is_fallback, ok, error, "
            "latency_ms, input_tokens, output_tokens, prompt_version, prompt_hash, settings) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(job, request_id, search_id, c["step"], c["model"], c["served_by"],
              bool(c["model"]) and c["model"] != assistant.model_name, c["ok"], c["error"], c["latency_ms"],
              c["input_tokens"], c["output_tokens"], version, fingerprint, Jsonb(assistant.settings()))
             for c in recorder.calls])


def log_failed_calls(job: str, recorder: CallRecorder, **ids) -> None:
    """Record the calls of a request that failed, outside its rolled-back transaction."""
    try:
        with db.connect() as conn:
            log_model_calls(conn, job, recorder, **ids)
    except Exception:
        pass


# ---------------------------------------------------------------- buyer side
# The filter bar and the chat share one SearchFilters object: the bar is sent
# with every chat message, and the chat's filters are written back to the bar.

def _num(value, cast=float):
    try:
        v = cast(str(value).replace(",", "").strip())
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def filters_from_form(p) -> SearchFilters:
    """Filter-bar fields (query string or JSON) to SearchFilters; bad values are ignored."""
    f = SearchFilters()
    if p.get("deal") in ("sale", "rent"):
        f.deal_type = p["deal"]
    t = p.get("type")
    if t in PROPERTY_TYPES:
        f.property_types = [t]
    if (p.get("area") or "").strip():
        f.location = str(p["area"]).strip()[:80]
    if p.get("state") in geo.STATES:
        f.state = p["state"]
    f.max_price = _num(p.get("max_price")) if p.get("max_price") else None
    f.min_land_acres = _num(p.get("min_acres")) if p.get("min_acres") else None
    f.min_power_amps = _num(p.get("min_power"), int) if p.get("min_power") else None
    f.min_ceiling_ft = _num(p.get("min_ceiling")) if p.get("min_ceiling") else None
    if p.get("tenure") in ("freehold", "leasehold"):
        f.tenure = p["tenure"]
    if p.get("container") in ("1", "on", "true", True):
        f.container_access = True
    return f


def form_from_filters(f: SearchFilters) -> dict:
    """The reverse, so the filter bar shows what the chat searched for."""
    types = set(f.property_types)
    t = f.property_types[0] if len(types) == 1 else ""
    out = {"deal": f.deal_type or "", "type": t, "area": f.location or "", "state": f.state or "",
           "max_price": f.max_price, "min_acres": f.min_land_acres, "min_power": f.min_power_amps,
           "min_ceiling": f.min_ceiling_ft, "tenure": f.tenure or "", "container": bool(f.container_access)}
    return {k: ("" if v is None else (number(v, 2) if isinstance(v, float) else v)) for k, v in out.items()}


def results_payload(results: list[dict]) -> dict:
    return {
        "count": len(results),
        "html": templates.get_template("portal/_cards.html").render(listings=results),
        "markers": [{"id": str(r["listing_id"]), "url": seo.listing_path(r), "lat": r["latitude"], "lng": r["longitude"],
                     "title": r["title"], "price": price_label(r)}
                    for r in results if r["latitude"] is not None],
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request, conn=Depends(db.get_conn)):
    filters = filters_from_form(request.query_params)
    listings = db.search_listings(conn, filters)
    vid = visitor_id(request)
    log_events(conn, [l["ad_id"] for l in listings], "impression", vid)
    if request.query_params:
        log_search(conn, vid, "filters", filters, len(listings))
    payload = results_payload(listings)
    filtered = bool(request.query_params)
    page = {
        "title": f"Factories, Warehouses & Land for Sale and Rent in Malaysia | {config.SITE_NAME}",
        "description": "Search factories, warehouses and industrial land across Malaysia by power supply, ceiling height, land size, "
                       "tenure and container access. Kilang, gudang dan tanah untuk dijual dan disewa.",
        "canonical": seo.absolute("/"),
        # Filter combinations are endless; keep them out of the index but let crawlers follow the links.
        "robots": "noindex, follow" if filtered else "index, follow",
        "jsonld": [seo.site_jsonld(), seo.organization_jsonld()],
    }
    return render(request, "portal/home.html", listings=listings, form=form_from_filters(filters),
                  filtered=filtered, markers=payload["markers"], page=page)


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)  # JSON-only, so cross-site forms cannot post here
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400)
    return body


@app.post("/api/search")
async def search(request: Request):
    filters = filters_from_form(await _json_body(request))
    vid = visitor_id(request)

    def run():
        with db.connect() as conn:
            results = db.search_listings(conn, filters)
            log_events(conn, [r["ad_id"] for r in results], "impression", vid)
            log_search(conn, vid, "filters", filters, len(results))
            return results

    return results_payload(await run_in_threadpool(run))


MAX_AUDIO_BYTES = 3 * 1024 * 1024   # 30 s of 16 kHz 16-bit mono WAV is about 1 MB
AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp4", "audio/aac", "audio/ogg",
               "audio/flac", "audio/webm"}


def _history(raw) -> list[dict]:
    history = [
        {"role": "user" if m.get("role") == "user" else "assistant", "content": str(m.get("content", ""))[:1000]}
        for m in (raw if isinstance(raw, list) else [])[-12:] if isinstance(m, dict)
    ]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "Send a message.")
    return history


async def _answer_chat(request: Request, history: list[dict], current: SearchFilters, method: str,
                       recorder: CallRecorder, image: tuple[bytes, str] | None = None, extra: dict | None = None):
    """Run the search loop for the newest buyer turn, log the search and the model calls, and shape the reply."""
    vid, typed = visitor_id(request), history[-1]["content"]

    def run():
        with db.connect() as conn:
            res = get_assistant().search_chat(history, lambda f: db.search_listings(conn, f), current, recorder,
                                              image=image)
            if res.results is not None:
                log_events(conn, [r["ad_id"] for r in res.results], "impression", vid)
            search_id = log_search(conn, vid, method, res.filters or current,
                                   len(res.results) if res.results is not None else None,
                                   typed, res.model_name, res.declined)
            log_model_calls(conn, "search_chat", recorder, search_id=search_id)
            return res

    try:
        res = await run_in_threadpool(run)
    except Exception:
        try:  # record the failed search too, so failure rates can be measured
            with db.connect() as conn:
                search_id = log_search(conn, vid, method, current, None, typed, "error")
                log_model_calls(conn, "search_chat", recorder, search_id=search_id)
        except Exception:
            pass
        return JSONResponse({"reply": "Sorry, search is unavailable right now. Please try again."}, status_code=502)

    out = {"reply": res.reply, **(extra or {})}
    if res.results is not None:
        out.update(results_payload(res.results), form=form_from_filters(res.filters))
    return out


def _chat_allowed(request: Request) -> bool:
    ip = request.client.host if request.client else "unknown"
    return security.CHAT_PER_VISITOR.allow(visitor_id(request)) and security.CHAT_PER_IP.allow(ip)


BUSY_REPLY = {"reply": "You are sending messages faster than we can answer. Please wait a few minutes, or use the filters."}


@app.post("/api/chat")
async def chat(request: Request):
    body = await _json_body(request)
    history = _history(body.get("messages"))
    if not history[-1]["content"].strip():
        raise HTTPException(400, "Send a message.")
    current = filters_from_form(body.get("filters") or {})
    if not _chat_allowed(request):
        return JSONResponse(BUSY_REPLY, status_code=429)
    return await _answer_chat(request, history, current, "text", CallRecorder())


@app.post("/api/chat/media")
async def chat_media(request: Request, messages: str = Form("[]"), filters: str = Form("{}"),
                     audio: UploadFile | None = File(None), photo: UploadFile | None = File(None)):
    """A voice message or a photo from the search box. The clip is transcribed first, so the words go
    through the same masking, logging and search loop as typed text; a photo is shown to the model
    with the newest message. Neither file is kept."""
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(415)  # same protection as the JSON-only endpoint: plain cross-site forms cannot post here
    try:
        body = {"messages": json.loads(messages), "filters": json.loads(filters)}
    except ValueError:
        raise HTTPException(400, "Bad request.")
    history = _history(body["messages"])
    current = filters_from_form(body["filters"] if isinstance(body["filters"], dict) else {})
    if not _chat_allowed(request):
        return JSONResponse(BUSY_REPLY, status_code=429)
    recorder = CallRecorder()

    if audio is not None and audio.filename:
        mime = (audio.content_type or "").split(";")[0]
        if mime not in AUDIO_TYPES:
            raise HTTPException(400, "Unsupported audio format.")
        clip = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(clip) > MAX_AUDIO_BYTES:
            raise HTTPException(400, "Voice messages are limited to 30 seconds.")
        try:
            transcript = await run_in_threadpool(get_assistant().transcribe, clip, mime, recorder)
        except Exception:
            transcript = ""
        if not transcript:
            log_failed_calls("transcribe", recorder)
            return JSONResponse({"reply": "I could not make out any words in that recording. Please try again "
                                          "or type what you need.", "heard": ""}, status_code=200)
        history[-1]["content"] = transcript[:1000]
        return await _answer_chat(request, history, current, "voice", recorder, extra={"heard": pii.mask(transcript)})

    if photo is not None and photo.filename:
        try:
            jpeg, _, _ = images.process_upload(await photo.read(config.MAX_IMAGE_BYTES + 1))
        except images.ImageError as exc:
            raise HTTPException(400, str(exc))
        caption = history[-1]["content"].strip()
        history[-1]["content"] = f"[photo] {caption}".strip()
        return await _answer_chat(request, history, current, "image", recorder, image=(jpeg, "image/jpeg"))

    raise HTTPException(400, "Attach a voice message or a photo.")


def _published_listing(conn, listing_id):
    rows = conn.execute(db.LISTING_CARD_SQL + " AND l.listing_id = %s", [listing_id]).fetchall()
    if not rows:
        # Once public but now sold, withdrawn or expired: 410 Gone, so search engines drop it quickly.
        ended = conn.execute("SELECT 1 FROM outcomes WHERE listing_id = %s", [listing_id]).fetchone()
        raise HTTPException(410 if ended else 404)
    return rows[0]


@app.get("/listings/{listing_id}")
def listing_short_url(listing_id: uuid.UUID, conn=Depends(db.get_conn)):
    """Old and short links redirect permanently to the readable URL."""
    return RedirectResponse(seo.listing_path(_published_listing(conn, listing_id)), status_code=301)


@app.get("/listings/{listing_id}/{slug}", response_class=HTMLResponse)
def listing_page(listing_id: uuid.UUID, slug: str, request: Request, conn=Depends(db.get_conn)):
    listing = _published_listing(conn, listing_id)
    path = seo.listing_path(listing)
    if request.url.path != path:  # title changed or slug mistyped: one canonical URL
        return RedirectResponse(path, status_code=301)
    photos = conn.execute("SELECT i.file_name, i.width, i.height, i.caption FROM listing_images i "
                          "JOIN listings l USING (listing_id) WHERE i.listing_id = %s "
                          "ORDER BY (i.position = l.cover_position) DESC, i.position", [listing_id]).fetchall()
    log_events(conn, [listing["ad_id"]], "click", visitor_id(request))

    kind = type_label(listing["property_type"])
    deal = "for rent" if listing["deal_type"] == "rent" else "for sale"
    where = ", ".join(dict.fromkeys(p for p in (listing["location"], listing["state"]) if p))  # dedupe, keep order
    specs = [s for s in (
        f"{number(listing['land_area_acres'], 2)} acres" if listing["land_area_acres"] else None,
        f"{number(listing['size_sqft'])} sqft built-up" if listing["size_sqft"] else None,
        f"{number(listing['power_amps'])}A" if listing["power_amps"] else (f"{number(listing['power_kva'])} kVA" if listing["power_kva"] else None),
        f"{number(listing['ceiling_height_ft'], 1)} ft ceiling" if listing["ceiling_height_ft"] else None,
        listing["tenure"],
        "container access" if listing["container_access"] else None,
    ) if s]
    crumbs = [("Home", "/")]
    landing = seo.landing_path(listing)
    if landing:
        crumbs.append((seo.LANDING[landing][2], f"/{landing}"))
        for place in (listing["state"], listing["location"]):
            if place and seo.place_from_slug(seo.slugify(place)) and place != (crumbs[-1][0].split(" in ")[-1]):
                crumbs.append((f"{seo.LANDING[landing][2]} in {place}", f"/{landing}/{seo.slugify(place)}"))
    crumbs.append((listing["title"], path))
    page = {
        "title": f"{listing['title']} – {price_label(listing)} | {config.SITE_NAME}",
        "description": f"{kind} {deal}{' in ' + where if where else ''}: {', '.join(specs) or 'see specifications'}. "
                       "View photos, full specifications and the map.",
        "canonical": seo.absolute(path),
        "robots": "index, follow",
        "og_image": seo.absolute(f"/uploads/{photos[0]['file_name']}") if photos else None,
        "og_type": "article",
        "jsonld": [seo.listing_jsonld(listing, [p["file_name"] for p in photos]), seo.breadcrumbs(crumbs)],
    }
    agent = conn.execute("SELECT a.display_name, a.registration_no FROM listings l JOIN real_estate_agents a "
                         "USING (real_estate_agent_id) WHERE l.listing_id = %s", [listing_id]).fetchone()
    return render(request, "portal/listing.html", listing=listing, photos=photos, page=page, crumbs=crumbs,
                  agent=agent)


@app.post("/api/listings/{listing_id}/phone")
def reveal_phone(listing_id: uuid.UUID, request: Request, conn=Depends(db.get_conn)):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403)  # custom header forces a CORS preflight for cross-site callers
    row = conn.execute(
        "SELECT a.phone, a.display_name, pa.ad_id FROM listings l "
        "JOIN real_estate_agents a USING (real_estate_agent_id) "
        "JOIN LATERAL (SELECT ad_id FROM published_ads p WHERE p.listing_id = l.listing_id "
        "              ORDER BY published_at DESC LIMIT 1) pa ON true "
        "WHERE l.listing_id = %s AND l.status = 'published' "
        "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.listing_id = l.listing_id)", [listing_id]).fetchone()
    if not row:
        raise HTTPException(404)
    log_events(conn, [row["ad_id"]], "phone_reveal", visitor_id(request))
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
    data = images.load(name, conn)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg")


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
        SELECT l.listing_id, l.title, l.status, l.deal_type, l.price_listed AS price, l.updated_at,
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


def _agent_profile(conn, agent_id):
    return conn.execute("SELECT display_name, phone, registration_no FROM real_estate_agents "
                        "WHERE real_estate_agent_id = %s", [agent_id]).fetchone()


@app.get("/agent/profile", response_class=HTMLResponse)
def profile_form(request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)):
    return render(request, "agent/profile.html", agent=_agent_profile(conn, agent_id))


@app.post("/agent/profile", response_class=HTMLResponse)
def save_profile(request: Request, agent_id: str = Depends(security.require_agent),
                 display_name: str = Form(""), phone: str = Form(""), registration_no: str = Form(""),
                 csrf: str = Form(None), conn=Depends(db.get_conn)):
    security.check_csrf(request, csrf)
    name, phone, reg = display_name.strip()[:80], phone.strip()[:30], registration_no.strip()[:40]
    if not name:
        agent = {"display_name": name, "phone": phone, "registration_no": reg}
        return render(request, "agent/profile.html", 400, agent=agent, error="Enter the name buyers should see.")
    if phone and not re.fullmatch(r"[+0-9][0-9 ()-]{6,29}", phone):
        agent = {"display_name": name, "phone": phone, "registration_no": reg}
        return render(request, "agent/profile.html", 400, agent=agent, error="Enter the phone number in digits, e.g. 012-345 6789.")
    conn.execute("UPDATE real_estate_agents SET display_name = %s, phone = %s, registration_no = %s "
                 "WHERE real_estate_agent_id = %s", [name, phone or None, reg or None, agent_id])
    return render(request, "agent/profile.html", agent=_agent_profile(conn, agent_id), saved=True)


@app.get("/agent/new", response_class=HTMLResponse)
def new_listing_form(request: Request, agent_id: str = Depends(security.require_agent)):
    return render(request, "agent/new.html", max_images=config.MAX_IMAGES)


def _create_request(conn, agent_id, listing_id, description: str, photos: list[bytes],
                    recorder: CallRecorder) -> uuid.UUID:
    """Call the model, then store the request, every draft in shuffled order, and the model calls."""
    started = time.monotonic()
    result = get_assistant().write_ads(description, [(p, "image/jpeg") for p in photos], config.DRAFTS_PER_REQUEST,
                                       recorder)
    return _store_request(conn, agent_id, listing_id, result, int((time.monotonic() - started) * 1000), recorder)


def _store_request(conn, agent_id, listing_id, result, latency_ms: int, recorder: CallRecorder) -> uuid.UUID:
    analysis = result.analysis
    request_id, seed = uuid.uuid4(), random.SystemRandom().getrandbits(63)
    conn.execute(
        "INSERT INTO generation_requests (request_id, listing_id, real_estate_agent_id, model_name, "
        "prompt_version, prompt_text, experiment_arm, order_seed, analysis) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [request_id, listing_id, agent_id, result.model_name, PROMPT_VERSION, result.prompt_text,
         EXPERIMENT_ARM, seed, Jsonb(analysis.model_dump(exclude={"drafts"}))])

    order = list(range(len(analysis.drafts)))
    random.Random(seed).shuffle(order)  # reproducible from the logged seed
    raw = analysis.model_dump()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO candidates (candidate_id, request_id, display_position, headline, ad_text, "
            "raw_response, latency_ms, cover_position) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(uuid.uuid4(), request_id, pos, analysis.drafts[i].headline, analysis.drafts[i].body,
              Jsonb({"generated_index": i, "analysis": raw, "check": result.checks[i]}), latency_ms,
              analysis.drafts[i].cover_photo)
             for pos, i in enumerate(order, 1)])
    log_model_calls(conn, "write_ads", recorder, request_id=request_id)
    return request_id


def _save_new_listing(agent_id, description: str, processed: list, result, latency_ms: int,
                      recorder: CallRecorder) -> uuid.UUID:
    """Listing, photos and the generation request in one transaction, after the model has answered."""
    with db.connect() as conn:
        listing_id = uuid.uuid4()
        conn.execute("INSERT INTO listings (listing_id, real_estate_agent_id, description_original) "
                     "VALUES (%s, %s, %s)", [listing_id, agent_id, description])
        for pos, (data, w, h) in enumerate(processed, 1):
            conn.execute("INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                         "VALUES (%s,%s,%s,%s,%s,%s)", [uuid.uuid4(), listing_id, pos, images.save(data, conn), w, h])
        return _store_request(conn, agent_id, listing_id, result, latency_ms, recorder)


def _stream_new_listing(agent_id, description: str, processed: list, recorder: CallRecorder):
    """NDJSON progress for the new-listing page: one line per finished graph step, then where to go."""
    async def lines():
        started = time.monotonic()
        yield json.dumps({"step": f"Masked contact details; reading {len(processed)} photo(s) and writing drafts…"}) + "\n"
        result = None
        try:
            async for kind, value in get_assistant().astream_ads(
                    description, [(d, "image/jpeg") for d, _, _ in processed], config.DRAFTS_PER_REQUEST, recorder):
                if kind == "step":
                    yield json.dumps({"step": value}) + "\n"
                else:
                    result = value
            yield json.dumps({"step": "Saving the drafts…"}) + "\n"
            request_id = await run_in_threadpool(_save_new_listing, agent_id, description, processed, result,
                                                 int((time.monotonic() - started) * 1000), recorder)
        except Exception as exc:
            log_failed_calls("write_ads", recorder)
            yield json.dumps({"error": f"The ad writer failed ({type(exc).__name__}). Nothing was saved; "
                                       "please try again."}) + "\n"
            return
        yield json.dumps({"done": True, "redirect": f"/agent/requests/{request_id}"}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/agent/new")
async def create_listing(request: Request, agent_id: str = Depends(security.require_agent),
                         description: str = Form(""), photos: list[UploadFile] = File([]),
                         brochure_pdf: UploadFile | None = File(None), csrf: str = Form(None)):
    security.check_csrf(request, csrf)
    description = description.strip()
    error = None
    if len(description) > 1500:
        error = "Keep your own description under 1,500 characters; put the rest in a brochure PDF."
    photos = [p for p in photos if p.filename]
    if not error and len(photos) > config.MAX_IMAGES:
        error = f"Upload at most {config.MAX_IMAGES} photos."

    # Brochure: its text joins the description (and is masked with it); its pictures fill the
    # remaining photo slots after the agent's own photos.
    brochure_text, brochure_images = "", []
    if not error and brochure_pdf is not None and brochure_pdf.filename:
        try:
            brochure_text, brochure_images = brochure.extract(
                await brochure_pdf.read(brochure.MAX_BYTES + 1), max_images=config.MAX_IMAGES - len(photos))
        except brochure.BrochureError as exc:
            error = str(exc)
    if not error and brochure_text:
        description = f"{description}\n\n[From the brochure]\n{brochure_text}" if description else brochure_text
    if not error and len(description) < 20:
        error = ("Write two or three sentences about the property (at least 20 characters), "
                 "or upload a brochure PDF that contains text.")
    processed = []
    if not error:
        try:
            for p in photos:
                processed.append(images.process_upload(await p.read(config.MAX_IMAGE_BYTES + 1)))
            for data in brochure_images:
                processed.append(images.process_upload(data))
        except images.ImageError as exc:
            error = str(exc)
    if not error and not processed:
        error = "Add at least one photo, or a brochure PDF with pictures in it."
    if error:
        return render(request, "agent/new.html", 400, error=error, description=description,
                      max_images=config.MAX_IMAGES)
    if not security.ADS_PER_AGENT.allow(agent_id):
        return render(request, "agent/new.html", 429, description=description, max_images=config.MAX_IMAGES,
                      error="You have reached today's limit for new ads. Please try again tomorrow.")

    recorder = CallRecorder()
    if "application/x-ndjson" in request.headers.get("accept", ""):
        return _stream_new_listing(agent_id, description, processed, recorder)  # the page shows live progress

    def run():  # without JavaScript: wait for the whole graph, then redirect
        started = time.monotonic()
        result = get_assistant().write_ads(description, [(d, "image/jpeg") for d, _, _ in processed],
                                           config.DRAFTS_PER_REQUEST, recorder)
        return _save_new_listing(agent_id, description, processed, result,
                                 int((time.monotonic() - started) * 1000), recorder)

    try:
        request_id = await run_in_threadpool(run)
    except Exception as exc:
        log_failed_calls("write_ads", recorder)
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
    # The latest successful revision (agent feedback) replaces the draft text and its check.
    drafts = conn.execute("""
        SELECT c.candidate_id, c.display_position, coalesce(r.headline, c.headline) AS headline,
               coalesce(r.ad_text, c.ad_text) AS ad_text, c.cover_position,
               CASE WHEN r.revision_id IS NULL THEN c.raw_response->'check'
                    ELSE jsonb_build_object('checked', r.unsupported_claims IS NOT NULL,
                                            'unsupported_claims', coalesce(r.unsupported_claims, '[]'::jsonb)) END AS check,
               r.feedback AS last_feedback,
               (SELECT count(*) FROM draft_revisions x WHERE x.candidate_id = c.candidate_id AND x.ok) AS revisions,
               (SELECT x.ok FROM draft_revisions x WHERE x.candidate_id = c.candidate_id
                 ORDER BY x.created_at DESC LIMIT 1) AS last_ok
        FROM candidates c
        LEFT JOIN LATERAL (SELECT * FROM draft_revisions r WHERE r.candidate_id = c.candidate_id AND r.ok
                           ORDER BY created_at DESC LIMIT 1) r ON true
        WHERE c.request_id = %s ORDER BY c.display_position""", [request_id]).fetchall()
    decision = conn.execute("SELECT * FROM decisions WHERE request_id = %s", [request_id]).fetchone()
    photos = conn.execute("SELECT position, file_name FROM listing_images WHERE listing_id = %s ORDER BY position",
                          [req["listing_id"]]).fetchall()
    request.session[f"opened:{request_id}"] = time.time()  # for seconds_to_decide
    analysis = req["analysis"] or {}
    card = analysis.get("card") or {}
    # First map pin: the named town's centre; the agent drags it to the exact spot.
    known = geo.locate(card.get("location"), card.get("title"), req["description_original"])
    pin = {"lat": known[1], "lng": known[2], "zoom": 13} if known else \
          {"lat": geo.MALAYSIA_CENTRE[0], "lng": geo.MALAYSIA_CENTRE[1], "zoom": 6}
    if known and not card.get("state"):
        card["state"] = known[0]
    captions = {c["photo"]: c["caption"] for c in analysis.get("photo_captions") or []}
    return render(request, "agent/review.html", req=req, analysis=analysis, card=card, drafts=drafts,
                  decision=decision, photos=photos, captions=captions, pin=pin, pin_set=bool(known))


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


def _opt_float(value: str | None, field: str, lo: float = 0, hi: float = 1e12) -> float | None:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        v = float(value)
    except ValueError:
        raise HTTPException(400, f"{field} must be a number.")
    if not lo <= v <= hi:
        raise HTTPException(400, f"{field} is out of range.")
    return v


def card_from_form(form) -> dict:
    """Validated listing columns from the review form, keyed by column name."""
    text = lambda k, n=120: (str(form.get(k, "")).strip()[:n] or None)
    property_type = form.get("property_type")
    if property_type not in PROPERTY_TYPES:
        raise HTTPException(400, "This site lists factories, warehouses, industrial or commercial land and whole commercial buildings only. "
                                 "Shoplots, shop offices and office units belong on kedai.my.")
    tenure = form.get("tenure") if form.get("tenure") in ("freehold", "leasehold") else None
    lat, lng = _opt_float(form.get("latitude"), "Latitude", -90, 90), _opt_float(form.get("longitude"), "Longitude", -180, 180)
    if (lat is None) != (lng is None):
        lat = lng = None
    return {
        "title": text("title"),
        "property_type": property_type,
        "deal_type": "rent" if form.get("deal_type") == "rent" else "sale",
        "location": text("location"),
        "state": form.get("state") if form.get("state") in geo.STATES else None,
        "price_listed": _opt_float(form.get("price"), "Price"),
        "land_area_acres": _opt_float(form.get("land_area_acres"), "Land area", 0.001, 100000),
        "size_sqft": _opt_int(form.get("size_sqft"), "Built-up area") or None,
        "ceiling_height_ft": _opt_float(form.get("ceiling_height_ft"), "Ceiling height", 1, 999),
        "power_amps": _opt_int(form.get("power_amps"), "Power (amps)") or None,
        "power_kva": _opt_int(form.get("power_kva"), "Power (kVA)") or None,
        "tenure": tenure,
        "lease_years_left": (_opt_int(form.get("lease_years_left"), "Lease years left") or None) if tenure == "leasehold" else None,
        "zoning": form.get("zoning") if form.get("zoning") in ZONINGS else None,
        "container_access": {"yes": True, "no": False}.get(form.get("container_access")),
        "loading_bays": _opt_int(form.get("loading_bays"), "Loading bays"),
        "features": sorted({f.strip().lower()[:40] for f in str(form.get("features", "")).split(",") if f.strip()})[:30],
        "latitude": lat,
        "longitude": lng,
    }


def ranks_from_form(form, candidate_ids: list) -> dict:
    """Optional creativity ranking: every draft ranked 1..n with no ties, or none at all."""
    given = {c: str(form.get(f"rank_{c}", "")).strip() for c in candidate_ids}
    if not any(given.values()):
        return {}
    try:
        ranks = {c: int(v) for c, v in given.items()}
    except ValueError:
        raise HTTPException(400, "Rank every draft, or leave the ranking empty.")
    if sorted(ranks.values()) != list(range(1, len(candidate_ids) + 1)):
        raise HTTPException(400, "Give each draft a different rank, from 1 (most creative) down.")
    return ranks


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

    card = card_from_form(form)
    candidate_ids = [r["candidate_id"] for r in conn.execute(
        "SELECT candidate_id FROM candidates WHERE request_id = %s", [request_id]).fetchall()]
    ranks = ranks_from_form(form, candidate_ids)

    # Cover photo: the agent's pick, else the kept draft's suggestion, else the first photo.
    n_photos = conn.execute("SELECT count(*) AS n FROM listing_images WHERE listing_id = %s",
                            [req["listing_id"]]).fetchone()["n"]
    picked = _opt_int(form.get("cover"), "cover photo")
    if picked is not None and not 1 <= picked <= n_photos:
        raise HTTPException(400, "Pick one of the photos as the cover.")
    cover = picked or (candidate and candidate["cover_position"]) or 1

    opened = request.session.pop(f"opened:{request_id}", None)
    final_text = f"{headline}\n\n{body}"
    if candidate:  # the agent edited the latest revision of the draft, if they asked for any
        shown = revisions.current_drafts(conn, request_id)
        shown = next(d for d in shown.values() if d["candidate_id"] == candidate["candidate_id"])
        candidate = {**candidate, "headline": shown["headline"], "ad_text": shown["ad_text"]}
    conn.execute(
        "INSERT INTO decisions (request_id, decision_type, kept_candidate_id, final_text, edit_distance, "
        "decided_at, seconds_to_decide, cover_position) VALUES (%s,%s,%s,%s,%s,now(),%s,%s)",
        [request_id, decision_type, candidate and candidate["candidate_id"], final_text,
         edit_distance(f"{candidate['headline']}\n\n{candidate['ad_text']}", final_text) if candidate else None,
         int(time.time() - opened) if opened else None, cover])
    with conn.cursor() as cur:  # captions from this request become the photos' alt text
        cur.executemany("UPDATE listing_images SET caption = %s WHERE listing_id = %s AND position = %s",
                        [(c["caption"][:200], req["listing_id"], c["photo"])
                         for c in (req["analysis"] or {}).get("photo_captions") or []])
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO rankings (request_id, candidate_id, rank) VALUES (%s,%s,%s)",
                        [(request_id, c, r) for c, r in ranks.items()])
    card["title"] = card["title"] or headline
    card["description_language"] = (req["analysis"] or {}).get("detected_language")
    conn.execute(
        "UPDATE listings SET status = 'published', updated_at = now(), cover_position = %s, "
        + ", ".join(f"{col} = %s" for col in card) + " WHERE listing_id = %s",
        [cover, *card.values(), req["listing_id"]])  # column names come from card_from_form, never from the request
    conn.execute("INSERT INTO published_ads (ad_id, request_id, listing_id, channel, published_at, headline, ad_text) "
                 "VALUES (%s,%s,%s,'own_portal',now(),%s,%s)",
                 [uuid.uuid4(), request_id, req["listing_id"], headline, body])
    conn.commit()  # before redirecting, so the listing page sees it
    embeddings.embed_missing(conn)  # meaning-based search; never blocks publishing
    return RedirectResponse(seo.listing_path({"listing_id": req["listing_id"], "title": card["title"]}), status_code=303)


@app.post("/agent/requests/{request_id}/drafts/{candidate_id}/revise")
async def revise_draft(request_id: uuid.UUID, candidate_id: uuid.UUID, request: Request,
                       agent_id: str = Depends(security.require_agent), feedback: str = Form(""),
                       csrf: str = Form(None)):
    """Human-in-the-loop: the agent's feedback resumes the paused revision graph for this request."""
    security.check_csrf(request, csrf)
    feedback = feedback.strip()
    if not 3 <= len(feedback) <= revisions.MAX_FEEDBACK:
        raise HTTPException(400, f"Write what to change (3 to {revisions.MAX_FEEDBACK} characters).")
    if not security.REVISIONS_PER_AGENT.allow(agent_id):
        raise HTTPException(429, "You have reached today's limit for revisions. Please try again tomorrow.")

    def run():
        revisions.ensure_setup()  # before opening a transaction (see ensure_setup)
        with db.connect() as conn:
            _load_request(conn, request_id, agent_id)
            if conn.execute("SELECT 1 FROM decisions WHERE request_id = %s", [request_id]).fetchone():
                raise HTTPException(409, "This request already has a decision.")
            if not conn.execute("SELECT 1 FROM candidates WHERE request_id = %s AND candidate_id = %s",
                                [request_id, candidate_id]).fetchone():
                raise HTTPException(404)
            return revisions.revise(conn, request_id, candidate_id, feedback)

    await run_in_threadpool(run)
    return RedirectResponse(f"/agent/requests/{request_id}#draft-{candidate_id}", status_code=303)


@app.post("/agent/requests/{request_id}/regenerate")
async def regenerate(request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent),
                     csrf: str = Form(None)):
    security.check_csrf(request, csrf)
    if not security.ADS_PER_AGENT.allow(agent_id):
        raise HTTPException(429, "You have reached today's limit for new drafts. Please try again tomorrow.")
    recorder = CallRecorder()

    def run():
        with db.connect() as conn:
            req = _load_request(conn, request_id, agent_id)
            if conn.execute("SELECT 1 FROM decisions WHERE request_id = %s", [request_id]).fetchone():
                raise HTTPException(409, "This request already has a decision.")
            conn.execute("INSERT INTO decisions (request_id, decision_type, decided_at) VALUES (%s,'kept_none',now())",
                         [request_id])
            names = conn.execute("SELECT file_name FROM listing_images WHERE listing_id = %s ORDER BY position",
                                 [req["listing_id"]]).fetchall()
            photos = [images.load(n["file_name"], conn) for n in names]
            return _create_request(conn, agent_id, req["listing_id"], req["description_original"], photos, recorder)

    try:
        new_id = await run_in_threadpool(run)
    except Exception:
        log_failed_calls("write_ads", recorder)
        raise
    return RedirectResponse(f"/agent/requests/{new_id}", status_code=303)


# ------------------------------------------------------------------ SEO
# Registered last so these catch-all paths never shadow the routes above.

@app.get("/favicon.ico")
def favicon():
    return FileResponse(config.ROOT / "app" / "static" / "icon-48.png", media_type="image/png")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(request, "privacy.html", operator=config.OPERATOR_NAME, privacy_email=config.PRIVACY_EMAIL,
                  search_logging=config.SEARCH_LOGGING, cookie_lifetime="8 hours", cookie_lifetime_bm="8 jam",
                  self_signup=False, has_map=True, ai_drafts=True, page={
                      "title": f"Privacy notice · Notis privasi | {config.SITE_NAME}",
                      "description": f"What personal data {config.SITE_NAME} collects, why, and your rights under Malaysia's PDPA.",
                      "canonical": seo.absolute('/privacy'), "robots": "index, follow"})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return (f"User-agent: *\nDisallow: /agent\nDisallow: /api/\n\nSitemap: {seo.absolute('/sitemap.xml')}\n")


def _xml(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


@app.get("/sitemap.xml")
def sitemap(conn=Depends(db.get_conn)):
    listings = conn.execute(
        "SELECT l.listing_id, l.title, l.property_type, l.deal_type, l.location, l.state, "
        "greatest(l.updated_at, max(p.published_at)) AS modified, "
        "array_agg(DISTINCT i.file_name) FILTER (WHERE i.file_name IS NOT NULL) AS photos "
        "FROM listings l JOIN published_ads p USING (listing_id) LEFT JOIN listing_images i USING (listing_id) "
        "WHERE l.status = 'published' AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.listing_id = l.listing_id) "
        "GROUP BY l.listing_id").fetchall()
    urls = [("/", None, [])]
    landings = {}
    for l in listings:
        urls.append((seo.listing_path(l), l["modified"], l["photos"] or []))
        path = seo.landing_path(l)
        if path:
            for key in {f"/{path}"} | {f"/{path}/{seo.slugify(pl)}" for pl in (l["location"], l["state"])
                                         if pl and seo.place_from_slug(seo.slugify(pl))}:
                landings[key] = max(filter(None, [landings.get(key), l["modified"]]))
    urls += [(path, modified, []) for path, modified in sorted(landings.items())]

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    for path, modified, photos in urls:
        out.append(f"<url><loc>{_xml(seo.absolute(path))}</loc>"
                   + (f"<lastmod>{modified.date().isoformat()}</lastmod>" if modified else "")
                   + "".join(f"<image:image><image:loc>{_xml(seo.absolute('/uploads/' + f))}</image:loc></image:image>"
                             for f in photos)
                   + "</url>")
    out.append("</urlset>")
    return Response("\n".join(out), media_type="application/xml")


def _range(values, fmt):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    lo, hi = fmt(values[0]), fmt(values[-1])
    return lo if lo == hi else f"{lo} to {hi}"


def landing_intro(heading: str, place: str, malay: str, listings: list[dict]) -> str:
    """A short summary built from the live listings, so each landing page says something specific."""
    n = len(listings)
    if not n:
        return (f"There are no {heading.lower()} in {place} listed right now. "
                "Try a nearby area, or describe what you need in the search box and we will show the closest matches.")
    parts = [f"{heading} in {place}: {n} listing{'s' if n != 1 else ''} available now."]
    price = _range([float(l["price"]) for l in listings if l["price"]], lambda v: money(v))
    if price:
        parts.append(f"Asking {'rent' if 'rent' in heading else 'price'}: {price}{' per month' if 'rent' in heading else ''}.")
    land = _range([float(l["land_area_acres"]) for l in listings if l["land_area_acres"]], lambda v: number(v, 2))
    if land:
        parts.append(f"Land sizes: {land} acres.")
    power = _range([l["power_amps_equiv"] for l in listings], lambda v: f"{number(v)} A")
    if power:
        parts.append(f"Power supply: {power}.")
    freehold = sum(1 for l in listings if l["tenure"] == "freehold")
    if freehold:
        parts.append("All freehold." if freehold == n else f"{freehold} of {n} freehold.")
    container = sum(1 for l in listings if l["container_access"])
    if container:
        parts.append("All with 40-ft container access." if container == n else f"{container} with 40-ft container access.")
    parts.append(f"{malay} di {place}.")
    return " ".join(parts)


def _landing(request: Request, conn, key: str, place_slug: str | None):
    if key not in seo.LANDING:
        raise HTTPException(404)
    types, deal, heading, malay = seo.LANDING[key]
    filters = SearchFilters(property_types=types, deal_type=deal)
    place = "Malaysia"
    path = f"/{key}"
    crumbs = [("Home", "/"), (heading, path)]
    if place_slug is not None:
        found = seo.place_from_slug(place_slug)
        if not found:
            raise HTTPException(404)
        kind, place = found
        if kind == "area":
            filters.location = place
            state = geo.AREAS[place][0]
            crumbs.append((f"{heading} in {state}", f"/{key}/{seo.slugify(state)}"))
        else:
            filters.state = place
        path = f"/{key}/{place_slug}"
        crumbs.append((f"{heading} in {place}", path))

    listings = db.search_listings(conn, filters)
    vid = visitor_id(request)
    log_events(conn, [l["ad_id"] for l in listings], "impression", vid)
    log_search(conn, vid, "browse", filters, len(listings))
    payload = results_payload(listings)

    # Related pages: same search in other places, and other searches here (only ones with listings).
    related_places, related_types = [], []
    for text, href in footer_links():
        if href.startswith(f"/{key}/") and href != path:
            related_places.append((text, href))
        elif place_slug and href.endswith(f"/{place_slug}") and not href.startswith(f"/{key}/"):
            related_types.append((text, href))

    n = len(listings)
    title_place = place if place_slug else "Malaysia"
    page = {
        "title": f"{heading} in {title_place}" + (f" ({n} listing{'s' if n != 1 else ''})" if n else "")
                 + f" | {config.SITE_NAME}",
        "description": landing_intro(heading, title_place, malay, listings)[:300],
        "canonical": seo.absolute(path),
        "robots": "index, follow" if n else "noindex, follow",  # no thin empty pages in the index
        "jsonld": [seo.breadcrumbs(crumbs), {
            "@context": "https://schema.org", "@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "position": i, "url": seo.absolute(seo.listing_path(l))}
                                for i, l in enumerate(listings, 1)]}],
    }
    return render(request, "portal/home.html", listings=listings, form=form_from_filters(filters), filtered=True,
                  markers=payload["markers"], page=page, crumbs=crumbs,
                  heading=f"{heading} in {title_place}", intro=landing_intro(heading, title_place, malay, listings),
                  related_places=related_places[:12], related_types=related_types[:8])


@app.get("/{key}", response_class=HTMLResponse)
def landing_country(key: str, request: Request, conn=Depends(db.get_conn)):
    return _landing(request, conn, key, None)


@app.get("/{key}/{place_slug}", response_class=HTMLResponse)
def landing_place(key: str, place_slug: str, request: Request, conn=Depends(db.get_conn)):
    return _landing(request, conn, key, place_slug)


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return render(request, "portal/message.html", 404, heading="Page not found",
                  text="This listing may have been taken down, or the link is mistyped.")


@app.exception_handler(410)
async def gone(request: Request, exc):
    return render(request, "portal/message.html", 410, heading="This property is no longer available",
                  text="It has been sold, rented out or withdrawn. Similar properties may still be listed.")
