"""Pages for search engines: privacy, robots.txt, the sitemap and the landing pages.

Include this router last: its /{key} paths would otherwise shadow the routes of the others.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from .. import config, db, geo, seo
from ..ai.schemas import SearchFilters
from ..services.tracking import log_events, log_search, visitor_id
from ..web.search_forms import form_from_filters, results_payload
from ..web.templating import footer_links, money, number, render

router = APIRouter()


@router.get("/favicon.ico")
def favicon():
    return FileResponse(config.ROOT / "app" / "static" / "icon-48.png", media_type="image/png")


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return render(
        request,
        "privacy.html",
        operator=config.OPERATOR_NAME,
        privacy_email=config.PRIVACY_EMAIL,
        search_logging=config.SEARCH_LOGGING,
        cookie_lifetime="8 hours",
        cookie_lifetime_bm="8 jam",
        self_signup=False,
        has_map=True,
        ai_drafts=True,
        page={
            "title": f"Privacy notice · Notis privasi | {config.SITE_NAME}",
            "description": f"What personal data {config.SITE_NAME} collects, why, and your rights under the PDPA.",
            "canonical": seo.absolute("/privacy"),
            "robots": "index, follow",
        },
    )


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    return f"User-agent: *\nDisallow: /agent\nDisallow: /api/\n\nSitemap: {seo.absolute('/sitemap.xml')}\n"


def _xml(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@router.get("/sitemap.xml")
def sitemap(conn=Depends(db.get_conn)):
    listings = conn.execute(
        "SELECT l.listing_id, l.title, l.property_type, l.deal_type, l.location, l.state, "
        "greatest(l.updated_at, max(p.published_at)) AS modified, "
        "array_agg(DISTINCT i.file_name) FILTER (WHERE i.file_name IS NOT NULL) AS photos "
        "FROM listings l JOIN published_ads p USING (listing_id) LEFT JOIN listing_images i USING (listing_id) "
        "WHERE l.status = 'published' AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.listing_id = l.listing_id) "
        "GROUP BY l.listing_id"
    ).fetchall()
    urls = [("/", None, [])]
    landings = {}
    for listing in listings:
        urls.append((seo.listing_path(listing), listing["modified"], listing["photos"] or []))
        path = seo.landing_path(listing)
        if path:
            # The listing's state, its location if that is a known place, and any known town named in the
            # location or title ("Temasya Glenmarie, Shah Alam" belongs under Glenmarie and Shah Alam).
            places = {listing["location"], listing["state"]} | geo.find_areas(
                f"{listing['location'] or ''} {listing['title'] or ''}"
            )
            for key in {f"/{path}"} | {
                f"/{path}/{seo.slugify(pl)}" for pl in places if pl and seo.place_from_slug(seo.slugify(pl))
            }:
                landings[key] = max(filter(None, [landings.get(key), listing["modified"]]))
    urls += [(path, modified, []) for path, modified in sorted(landings.items())]

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">',
    ]
    for path, modified, photos in urls:
        out.append(
            f"<url><loc>{_xml(seo.absolute(path))}</loc>"
            + (f"<lastmod>{modified.date().isoformat()}</lastmod>" if modified else "")
            + "".join(
                f"<image:image><image:loc>{_xml(seo.absolute('/uploads/' + f))}</image:loc></image:image>"
                for f in photos
            )
            + "</url>"
        )
    out.append("</urlset>")
    return Response("\n".join(out), media_type="application/xml")


def _range(values, fmt):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    lo, hi = fmt(values[0]), fmt(values[-1])
    return lo if lo == hi else f"{lo} to {hi}"


def landing_intro(heading: str, place: str | None, malay: str, listings: list[dict]) -> str:
    """A short summary built from the live listings, so each landing page says something specific."""
    n = len(listings)
    where = f" in {place}" if place else ""
    if not n:
        return (
            f"There are no {heading.lower()}{where} listed right now. "
            "Try a nearby area, or describe what you need in the search box and we will show the closest matches."
        )
    parts = [f"{heading}{where}: {n} listing{'s' if n != 1 else ''} available now."]
    price = _range([float(listing["price"]) for listing in listings if listing["price"]], lambda v: money(v))
    if price:
        parts.append(
            f"Asking {'rent' if 'rent' in heading else 'price'}: {price}{' per month' if 'rent' in heading else ''}."
        )
    land = _range(
        [float(listing["land_area_acres"]) for listing in listings if listing["land_area_acres"]],
        lambda v: number(v, 2),
    )
    if land:
        parts.append(f"Land sizes: {land} acres.")
    power = _range([listing["power_amps_equiv"] for listing in listings], lambda v: f"{number(v)} A")
    if power:
        parts.append(f"Power supply: {power}.")
    freehold = sum(1 for listing in listings if listing["tenure"] == "freehold")
    if freehold:
        parts.append("All freehold." if freehold == n else f"{freehold} of {n} freehold.")
    container = sum(1 for listing in listings if listing["container_access"])
    if container:
        parts.append(
            "All with 40-ft container access." if container == n else f"{container} with 40-ft container access."
        )
    parts.append(f"{malay} di {place}." if place else f"{malay}.")
    return " ".join(parts)


def _landing(request: Request, conn, key: str, place_slug: str | None):
    if key not in seo.LANDING:
        raise HTTPException(404)
    types, deal, heading, malay = seo.LANDING[key]
    filters = SearchFilters(property_types=types, deal_type=deal)
    place = None
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
    log_events(conn, [listing["ad_id"] for listing in listings], "impression", vid)
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
    title_place = place if place_slug else None
    full_heading = f"{heading} in {title_place}" if title_place else heading
    page = {
        "title": full_heading + (f" ({n} listing{'s' if n != 1 else ''})" if n else "") + f" | {config.SITE_NAME}",
        "description": landing_intro(heading, title_place, malay, listings)[:300],
        "canonical": seo.absolute(path),
        "robots": "index, follow" if n else "noindex, follow",  # no thin empty pages in the index
        "jsonld": [
            seo.breadcrumbs(crumbs),
            {
                "@context": "https://schema.org",
                "@type": "ItemList",
                "itemListElement": [
                    {"@type": "ListItem", "position": i, "url": seo.absolute(seo.listing_path(listing))}
                    for i, listing in enumerate(listings, 1)
                ],
            },
        ],
    }
    return render(
        request,
        "portal/home.html",
        listings=listings,
        form=form_from_filters(filters),
        filtered=True,
        markers=payload["markers"],
        page=page,
        crumbs=crumbs,
        heading=full_heading,
        intro=landing_intro(heading, title_place, malay, listings),
        related_places=related_places[:12],
        related_types=related_types[:8],
    )


@router.get("/{key}", response_class=HTMLResponse)
def landing_country(key: str, request: Request, conn=Depends(db.get_conn)):
    return _landing(request, conn, key, None)


@router.get("/{key}/{place_slug}", response_class=HTMLResponse)
def landing_place(key: str, place_slug: str, request: Request, conn=Depends(db.get_conn)):
    return _landing(request, conn, key, place_slug)
