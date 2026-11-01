"""Public pages: the home page, listing pages, the phone reveal, and listing photos and videos."""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import config, db, security, seo
from ..services import images, video
from ..services.tracking import log_events, log_search, visitor_id
from ..web.search_forms import filters_from_form, form_from_filters, results_payload
from ..web.templating import number, price_label, render, type_label

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn=Depends(db.get_conn)):
    filters = filters_from_form(request.query_params)
    listings = db.search_listings(conn, filters)
    vid = visitor_id(request)
    log_events(conn, [listing["ad_id"] for listing in listings], "impression", vid)
    if request.query_params:
        log_search(conn, vid, "filters", filters, len(listings))
    payload = results_payload(listings)
    filtered = bool(request.query_params)
    page = {
        "title": f"Factories, Warehouses & Land for Sale and Rent | {config.SITE_NAME}",
        "description": "Factories, warehouses, industrial land and commercial buildings for sale and rent in Shah Alam, "
        "Glenmarie, Puchong, Klang and more. Kilang, gudang dan tanah untuk dijual dan disewa.",
        "canonical": seo.absolute("/"),
        # Filter combinations are endless; keep them out of the index but let crawlers follow the links.
        "robots": "noindex, follow" if filtered else "index, follow",
        "jsonld": [seo.site_jsonld(), seo.organization_jsonld()],
    }
    featured = [] if filtered else db.featured_listings(conn)
    return render(
        request,
        "portal/home.html",
        listings=listings,
        form=form_from_filters(filters),
        filtered=filtered,
        markers=payload["markers"],
        page=page,
        featured=featured,
    )


def _published_listing(conn, listing_id):
    rows = conn.execute(db.LISTING_CARD_SQL + " AND l.listing_id = %s", [listing_id]).fetchall()
    if not rows:
        # Once public but now sold, withdrawn or expired: 410 Gone, so search engines drop it quickly.
        ended = conn.execute("SELECT 1 FROM outcomes WHERE listing_id = %s", [listing_id]).fetchone()
        raise HTTPException(410 if ended else 404)
    return rows[0]


@router.get("/listings/{listing_id}")
def listing_short_url(listing_id: uuid.UUID, conn=Depends(db.get_conn)):
    """Old and short links redirect permanently to the readable URL."""
    return RedirectResponse(seo.listing_path(_published_listing(conn, listing_id)), status_code=301)


@router.get("/listings/{listing_id}/{slug}", response_class=HTMLResponse)
def listing_page(listing_id: uuid.UUID, slug: str, request: Request, conn=Depends(db.get_conn)):
    listing = _published_listing(conn, listing_id)
    path = seo.listing_path(listing)
    if request.url.path != path:  # title changed or slug mistyped: one canonical URL
        return RedirectResponse(path, status_code=301)
    photos = conn.execute(
        "SELECT i.file_name, i.width, i.height, i.caption FROM listing_images i "
        "JOIN listings l USING (listing_id) WHERE i.listing_id = %s "
        "ORDER BY (i.position = l.cover_position) DESC, i.position",
        [listing_id],
    ).fetchall()
    log_events(conn, [listing["ad_id"]], "click", visitor_id(request))

    kind = type_label(listing["property_type"])
    deal = "for rent" if listing["deal_type"] == "rent" else "for sale"
    where = ", ".join(dict.fromkeys(p for p in (listing["location"], listing["state"]) if p))  # dedupe, keep order
    specs = [
        s
        for s in (
            f"{number(listing['land_area_acres'], 2)} acres" if listing["land_area_acres"] else None,
            f"{number(listing['size_sqft'])} sqft built-up" if listing["size_sqft"] else None,
            f"{number(listing['power_amps'])}A"
            if listing["power_amps"]
            else (f"{number(listing['power_kva'])} kVA" if listing["power_kva"] else None),
            f"{number(listing['ceiling_height_ft'], 1)} ft ceiling" if listing["ceiling_height_ft"] else None,
            listing["tenure"],
            "container access" if listing["container_access"] else None,
        )
        if s
    ]
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
    agent = conn.execute(
        "SELECT a.display_name, a.registration_no FROM listings l JOIN real_estate_agents a "
        "USING (real_estate_agent_id) WHERE l.listing_id = %s",
        [listing_id],
    ).fetchone()
    tour = conn.execute(
        "SELECT v.file_name FROM listing_videos v JOIN listings l USING (listing_id) "
        "WHERE v.listing_id = %s AND v.file_name IS NOT NULL AND l.video_enabled",
        [listing_id],
    ).fetchone()
    return render(
        request, "portal/listing.html", listing=listing, photos=photos, page=page, crumbs=crumbs, agent=agent, tour=tour
    )


@router.post("/api/listings/{listing_id}/phone")
def reveal_phone(listing_id: uuid.UUID, request: Request, conn=Depends(db.get_conn)):
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403)  # custom header forces a CORS preflight for cross-site callers
    row = conn.execute(
        "SELECT a.phone, a.display_name, pa.ad_id FROM listings l "
        "JOIN real_estate_agents a USING (real_estate_agent_id) "
        "JOIN LATERAL (SELECT ad_id FROM published_ads p WHERE p.listing_id = l.listing_id "
        "              ORDER BY published_at DESC LIMIT 1) pa ON true "
        "WHERE l.listing_id = %s AND l.status = 'published' "
        "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.listing_id = l.listing_id)",
        [listing_id],
    ).fetchone()
    if not row:
        raise HTTPException(404)
    log_events(conn, [row["ad_id"]], "phone_reveal", visitor_id(request))
    conn.commit()
    return {"phone": row["phone"] or "Not provided", "name": row["display_name"]}


UPLOAD_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")


@router.get("/uploads/{name}")
def uploaded_photo(name: str, request: Request, conn=Depends(db.get_conn)):
    if not UPLOAD_NAME.match(name):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT l.status, l.real_estate_agent_id FROM listing_images i JOIN listings l USING (listing_id) "
        "WHERE i.file_name = %s",
        [name],
    ).fetchone()
    # Photos of unpublished listings are visible only to the listing's agent.
    if not row or (
        row["status"] != "published" and str(row["real_estate_agent_id"]) != security.current_agent_id(request)
    ):
        raise HTTPException(404)
    data = images.load(name, conn)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg")


VIDEO_NAME = re.compile(r"^[0-9a-f]{32}\.mp4$")


@router.get("/videos/{name}")
def listing_video(name: str, conn=Depends(db.get_conn)):
    """FileResponse answers Range requests, which Safari requires before it will play a video.
    Identity encoding keeps the gzip middleware from compressing an already compressed file."""
    if not VIDEO_NAME.match(name):
        raise HTTPException(404)
    row = conn.execute(
        "SELECT 1 FROM listing_videos v JOIN listings l USING (listing_id) "
        "WHERE v.file_name = %s AND l.status = 'published' AND l.video_enabled",
        [name],
    ).fetchone()
    if not row:
        raise HTTPException(404)
    path = video.cached_path(name, conn)
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", headers={"Content-Encoding": "identity"})
