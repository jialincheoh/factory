"""The real estate agent's account: sign-in, dashboard, profile and per-listing switches."""

import re
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import db, security
from ..services import market, video
from ..web.templating import render

router = APIRouter()


@router.get("/agent/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "agent/login.html")


@router.post("/agent/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(None),
    conn=Depends(db.get_conn),
):
    security.check_csrf(request, csrf)
    ip = request.client.host if request.client else "unknown"
    if not security.login_allowed(ip):
        return render(request, "agent/login.html", 429, error="Too many attempts. Try again in 15 minutes.")
    row = conn.execute(
        "SELECT real_estate_agent_id, password_hash FROM real_estate_agents WHERE lower(email) = lower(%s)",
        [email.strip()],
    ).fetchone()
    if not security.verify_password(row["password_hash"] if row else None, password):
        security.record_failed_login(ip)
        return render(request, "agent/login.html", 400, error="Email or password is incorrect.", email=email)
    request.session.clear()  # new session on sign-in
    request.session["agent_id"] = str(row["real_estate_agent_id"])
    return RedirectResponse("/agent", status_code=303)


@router.post("/agent/logout")
def logout(request: Request, csrf: str = Form(None)):
    security.check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@router.get("/agent", response_class=HTMLResponse)
def dashboard(request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)):
    agent = conn.execute(
        "SELECT display_name FROM real_estate_agents WHERE real_estate_agent_id = %s", [agent_id]
    ).fetchone()
    listings = conn.execute(
        """
        SELECT l.listing_id, l.title, l.status, l.deal_type, l.price_listed AS price, l.updated_at, l.featured_at,
               l.video_enabled, v.status AS video_status, m.status AS market_status,
               (SELECT count(*) FROM listing_images i WHERE i.listing_id = l.listing_id) AS photo_count,
               (SELECT request_id FROM generation_requests g WHERE g.listing_id = l.listing_id
                 ORDER BY created_at DESC LIMIT 1) AS latest_request,
               count(e.*) FILTER (WHERE e.event_type = 'impression')   AS impressions,
               count(e.*) FILTER (WHERE e.event_type = 'click')        AS clicks,
               count(e.*) FILTER (WHERE e.event_type = 'phone_reveal') AS phone_reveals
        FROM listings l
        LEFT JOIN published_ads p ON p.listing_id = l.listing_id
        LEFT JOIN ad_events e ON e.ad_id = p.ad_id
        LEFT JOIN listing_videos v ON v.listing_id = l.listing_id
        LEFT JOIN listing_market_checks m ON m.listing_id = l.listing_id
        WHERE l.real_estate_agent_id = %s
        GROUP BY l.listing_id, v.status, m.status ORDER BY l.updated_at DESC""",
        [agent_id],
    ).fetchall()
    return render(request, "agent/dashboard.html", agent=agent, listings=listings, video_min_photos=video.MIN_PHOTOS)


def _agent_profile(conn, agent_id):
    return conn.execute(
        "SELECT display_name, phone, registration_no FROM real_estate_agents WHERE real_estate_agent_id = %s",
        [agent_id],
    ).fetchone()


@router.get("/agent/profile", response_class=HTMLResponse)
def profile_form(request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)):
    return render(request, "agent/profile.html", agent=_agent_profile(conn, agent_id))


@router.post("/agent/profile", response_class=HTMLResponse)
def save_profile(
    request: Request,
    agent_id: str = Depends(security.require_agent),
    display_name: str = Form(""),
    phone: str = Form(""),
    registration_no: str = Form(""),
    csrf: str = Form(None),
    conn=Depends(db.get_conn),
):
    security.check_csrf(request, csrf)
    name, phone, reg = display_name.strip()[:80], phone.strip()[:30], registration_no.strip()[:40]
    if not name:
        agent = {"display_name": name, "phone": phone, "registration_no": reg}
        return render(request, "agent/profile.html", 400, agent=agent, error="Enter the name buyers should see.")
    if phone and not re.fullmatch(r"[+0-9][0-9 ()-]{6,29}", phone):
        agent = {"display_name": name, "phone": phone, "registration_no": reg}
        return render(
            request,
            "agent/profile.html",
            400,
            agent=agent,
            error="Enter the phone number in digits, e.g. 012-345 6789.",
        )
    conn.execute(
        "UPDATE real_estate_agents SET display_name = %s, phone = %s, registration_no = %s "
        "WHERE real_estate_agent_id = %s",
        [name, phone or None, reg or None, agent_id],
    )
    return render(request, "agent/profile.html", agent=_agent_profile(conn, agent_id), saved=True)


@router.post("/agent/listings/{listing_id}/feature")
def toggle_featured(
    listing_id: uuid.UUID,
    request: Request,
    agent_id: str = Depends(security.require_agent),
    csrf: str = Form(None),
    conn=Depends(db.get_conn),
):
    """Put one of the agent's published listings in, or take it out of, the homepage's featured section."""
    security.check_csrf(request, csrf)
    row = conn.execute(
        "UPDATE listings SET featured_at = CASE WHEN featured_at IS NULL THEN now() END "
        "WHERE listing_id = %s AND real_estate_agent_id = %s AND status = 'published' RETURNING listing_id",
        [listing_id, agent_id],
    ).fetchone()
    if not row:
        raise HTTPException(404)
    return RedirectResponse("/agent", status_code=303)


@router.post("/agent/listings/{listing_id}/video")
def toggle_video(
    listing_id: uuid.UUID,
    request: Request,
    agent_id: str = Depends(security.require_agent),
    csrf: str = Form(None),
    conn=Depends(db.get_conn),
):
    """Turn the video tour of one of the agent's published listings on or off. Off hides it at once
    and keeps the film, so turning it back on needs no new render. Turning on gives a film that
    failed a fresh set of attempts."""
    security.check_csrf(request, csrf)
    row = conn.execute(
        "SELECT video_enabled, (SELECT count(*) FROM listing_images i "
        "WHERE i.listing_id = l.listing_id) AS photos FROM listings l "
        "WHERE listing_id = %s AND real_estate_agent_id = %s AND status = 'published'",
        [listing_id, agent_id],
    ).fetchone()
    if not row:
        raise HTTPException(404)
    if not row["video_enabled"] and row["photos"] < video.MIN_PHOTOS:
        raise HTTPException(400, f"A video needs at least {video.MIN_PHOTOS} photos.")
    conn.execute("UPDATE listings SET video_enabled = NOT video_enabled WHERE listing_id = %s", [listing_id])
    if not row["video_enabled"]:
        conn.execute("UPDATE listing_videos SET attempts = 0 WHERE listing_id = %s AND status = 'failed'", [listing_id])
    return RedirectResponse("/agent", status_code=303)


@router.get("/agent/listings/{listing_id}/market", response_class=HTMLResponse)
def market_check_page(
    listing_id: uuid.UUID, request: Request, agent_id: str = Depends(security.require_agent), conn=Depends(db.get_conn)
):
    """The comparison with similar listings online. For the listing's agent only: see app/services/market.py."""
    listing = conn.execute(
        "SELECT title FROM listings WHERE listing_id = %s AND real_estate_agent_id = %s AND status = 'published'",
        [listing_id, agent_id],
    ).fetchone()
    if not listing:
        raise HTTPException(404)
    check = conn.execute(
        "SELECT status, report FROM listing_market_checks WHERE listing_id = %s", [listing_id]
    ).fetchone()
    return render(
        request,
        "agent/market.html",
        listing=listing,
        check=check,
        report=check and check["report"],
        min_comparables=market.MIN_COMPARABLES,
    )
