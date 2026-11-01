"""Writing an ad: new listing, reviewing the drafts, revising them, and publishing."""

import json
import time
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import config, db, geo, security, seo
from ..ai import get_assistant
from ..ai.calllog import CallRecorder
from ..services import brochure, embeddings, images, revisions
from ..services.ad_requests import create_request, publish_ad, save_new_listing
from ..services.tracking import log_failed_calls
from ..web.listing_form import card_from_form, opt_int, ranks_from_form
from ..web.templating import render

router = APIRouter()


@router.get("/agent/new", response_class=HTMLResponse)
def new_listing_form(request: Request, agent_id: str = Depends(security.require_agent)):
    return render(request, "agent/new.html", max_images=config.MAX_IMAGES)


def _stream_new_listing(agent_id, description: str, processed: list, recorder: CallRecorder):
    """NDJSON progress for the new-listing page: one line per finished graph step, then where to go."""

    async def lines():
        started = time.monotonic()
        yield (
            json.dumps({"step": f"Masked contact details; reading {len(processed)} photo(s) and writing drafts…"})
            + "\n"
        )
        result = None
        try:
            async for kind, value in get_assistant().astream_ads(
                description, [(d, "image/jpeg") for d, _, _ in processed], config.DRAFTS_PER_REQUEST, recorder
            ):
                if kind == "step":
                    yield json.dumps({"step": value}) + "\n"
                else:
                    result = value
            yield json.dumps({"step": "Saving the drafts…"}) + "\n"
            request_id = await run_in_threadpool(
                save_new_listing,
                agent_id,
                description,
                processed,
                result,
                int((time.monotonic() - started) * 1000),
                recorder,
            )
        except Exception as exc:
            log_failed_calls("write_ads", recorder)
            yield (
                json.dumps(
                    {"error": f"The ad writer failed ({type(exc).__name__}). Nothing was saved; please try again."}
                )
                + "\n"
            )
            return
        yield json.dumps({"done": True, "redirect": f"/agent/requests/{request_id}"}) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


@router.post("/agent/new")
async def create_listing(
    request: Request,
    agent_id: str = Depends(security.require_agent),
    description: str = Form(""),
    photos: list[UploadFile] = File([]),
    brochure_pdf: UploadFile | None = File(None),
    csrf: str = Form(None),
):
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
                await brochure_pdf.read(brochure.MAX_BYTES + 1), max_images=config.MAX_IMAGES - len(photos)
            )
        except brochure.BrochureError as exc:
            error = str(exc)
    if not error and brochure_text:
        description = f"{description}\n\n[From the brochure]\n{brochure_text}" if description else brochure_text
    if not error and len(description) < 20:
        error = (
            "Write two or three sentences about the property (at least 20 characters), "
            "or upload a brochure PDF that contains text."
        )
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
        return render(
            request, "agent/new.html", 400, error=error, description=description, max_images=config.MAX_IMAGES
        )
    if not security.ADS_PER_AGENT.allow(agent_id):
        return render(
            request,
            "agent/new.html",
            429,
            description=description,
            max_images=config.MAX_IMAGES,
            error="You have reached today's limit for new ads. Please try again tomorrow.",
        )

    recorder = CallRecorder()
    if "application/x-ndjson" in request.headers.get("accept", ""):
        return _stream_new_listing(agent_id, description, processed, recorder)  # the page shows live progress

    def run():  # without JavaScript: wait for the whole graph, then redirect
        started = time.monotonic()
        result = get_assistant().write_ads(
            description, [(d, "image/jpeg") for d, _, _ in processed], config.DRAFTS_PER_REQUEST, recorder
        )
        return save_new_listing(
            agent_id, description, processed, result, int((time.monotonic() - started) * 1000), recorder
        )

    try:
        request_id = await run_in_threadpool(run)
    except Exception as exc:
        log_failed_calls("write_ads", recorder)
        return render(
            request,
            "agent/new.html",
            502,
            description=description,
            max_images=config.MAX_IMAGES,
            error=f"The ad writer failed ({type(exc).__name__}). Nothing was saved; please try again.",
        )
    return RedirectResponse(f"/agent/requests/{request_id}", status_code=303)


def _load_request(conn, request_id, agent_id):
    req = conn.execute(
        "SELECT g.*, l.description_original, l.status AS listing_status FROM generation_requests g "
        "JOIN listings l USING (listing_id) WHERE g.request_id = %s AND g.real_estate_agent_id = %s",
        [request_id, agent_id],
    ).fetchone()
    if not req:
        raise HTTPException(404)
    return req


@router.get("/agent/requests/{request_id}", response_class=HTMLResponse)
def review(
    request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)
):
    req = _load_request(conn, request_id, agent_id)
    # The latest successful revision (agent feedback) replaces the draft text and its check.
    drafts = conn.execute(
        """
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
        WHERE c.request_id = %s ORDER BY c.display_position""",
        [request_id],
    ).fetchall()
    decision = conn.execute("SELECT * FROM decisions WHERE request_id = %s", [request_id]).fetchone()
    photos = conn.execute(
        "SELECT position, file_name FROM listing_images WHERE listing_id = %s ORDER BY position", [req["listing_id"]]
    ).fetchall()
    request.session[f"opened:{request_id}"] = time.time()  # for seconds_to_decide
    analysis = req["analysis"] or {}
    card = analysis.get("card") or {}
    # First map pin: the named town's centre; the agent drags it to the exact spot.
    known = geo.locate(card.get("location"), card.get("title"), req["description_original"])
    pin = (
        {"lat": known[1], "lng": known[2], "zoom": 13}
        if known
        else {"lat": geo.COUNTRY_CENTRE[0], "lng": geo.COUNTRY_CENTRE[1], "zoom": 6}
    )
    if known and not card.get("state"):
        card["state"] = known[0]
    captions = {c["photo"]: c["caption"] for c in analysis.get("photo_captions") or []}
    return render(
        request,
        "agent/review.html",
        req=req,
        analysis=analysis,
        card=card,
        drafts=drafts,
        decision=decision,
        photos=photos,
        captions=captions,
        pin=pin,
        pin_set=bool(known),
    )


@router.post("/agent/requests/{request_id}/publish")
async def publish(
    request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)
):
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
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE request_id = %s AND candidate_id = %s", [request_id, uuid.UUID(choice)]
            ).fetchone()
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
    candidate_ids = [
        r["candidate_id"]
        for r in conn.execute("SELECT candidate_id FROM candidates WHERE request_id = %s", [request_id]).fetchall()
    ]
    ranks = ranks_from_form(form, candidate_ids)

    # Cover photo: the agent's pick, else the kept draft's suggestion, else the first photo.
    n_photos = conn.execute(
        "SELECT count(*) AS n FROM listing_images WHERE listing_id = %s", [req["listing_id"]]
    ).fetchone()["n"]
    picked = opt_int(form.get("cover"), "cover photo")
    if picked is not None and not 1 <= picked <= n_photos:
        raise HTTPException(400, "Pick one of the photos as the cover.")
    cover = picked or (candidate and candidate["cover_position"]) or 1

    opened = request.session.pop(f"opened:{request_id}", None)
    card["title"] = card["title"] or headline
    publish_ad(
        conn,
        request_id,
        req,
        candidate,
        decision_type,
        headline,
        body,
        card,
        ranks,
        cover,
        seconds_to_decide=int(time.time() - opened) if opened else None,
    )
    conn.commit()  # before redirecting, so the listing page sees it
    embeddings.embed_missing(conn)  # meaning-based search; never blocks publishing
    return RedirectResponse(
        seo.listing_path({"listing_id": req["listing_id"], "title": card["title"]}), status_code=303
    )


@router.post("/agent/requests/{request_id}/drafts/{candidate_id}/revise")
async def revise_draft(
    request_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    agent_id: str = Depends(security.require_agent),
    feedback: str = Form(""),
    csrf: str = Form(None),
):
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
            if not conn.execute(
                "SELECT 1 FROM candidates WHERE request_id = %s AND candidate_id = %s", [request_id, candidate_id]
            ).fetchone():
                raise HTTPException(404)
            return revisions.revise(conn, request_id, candidate_id, feedback)

    await run_in_threadpool(run)
    return RedirectResponse(f"/agent/requests/{request_id}#draft-{candidate_id}", status_code=303)


@router.post("/agent/requests/{request_id}/regenerate")
async def regenerate(
    request_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent), csrf: str = Form(None)
):
    security.check_csrf(request, csrf)
    if not security.ADS_PER_AGENT.allow(agent_id):
        raise HTTPException(429, "You have reached today's limit for new drafts. Please try again tomorrow.")
    recorder = CallRecorder()

    def run():
        with db.connect() as conn:
            req = _load_request(conn, request_id, agent_id)
            if conn.execute("SELECT 1 FROM decisions WHERE request_id = %s", [request_id]).fetchone():
                raise HTTPException(409, "This request already has a decision.")
            conn.execute(
                "INSERT INTO decisions (request_id, decision_type, decided_at) VALUES (%s,'kept_none',now())",
                [request_id],
            )
            names = conn.execute(
                "SELECT file_name FROM listing_images WHERE listing_id = %s ORDER BY position", [req["listing_id"]]
            ).fetchall()
            photos = [images.load(n["file_name"], conn) for n in names]
            return create_request(conn, agent_id, req["listing_id"], req["description_original"], photos, recorder)

    try:
        new_id = await run_in_threadpool(run)
    except Exception:
        log_failed_calls("write_ads", recorder)
        raise
    return RedirectResponse(f"/agent/requests/{new_id}", status_code=303)
