"""Jinja templates, their filters and globals, and the render() helper every page goes through."""

import logging
import time

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .. import config, db, geo, security, seo
from ..ai.schemas import PROPERTY_TYPES, ZONINGS

log = logging.getLogger("factory.web")

STATIC_DIR = config.ROOT / "app" / "static"
# Changes whenever a static file changes, so browsers can cache /static for a year.
STATIC_VERSION = str(int(max(f.stat().st_mtime for f in STATIC_DIR.rglob("*") if f.is_file())))
templates = Jinja2Templates(directory=config.ROOT / "app" / "templates")  # autoescapes HTML

# OpenStreetMap tiles are the only third-party resource; Leaflet itself is served from /static.
MAP_TILES = "https://tile.openstreetmap.org"


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
templates.env.globals.update(
    STATES=geo.STATES,
    AREAS=sorted(geo.AREAS),
    PROPERTY_TYPES=PROPERTY_TYPES,
    ZONINGS=ZONINGS,
    MAP_TILES=MAP_TILES,
    currency=config.CURRENCY,
    SITE_NAME=config.SITE_NAME,
    listing_path=seo.listing_path,
    absolute=seo.absolute,
    seo_script=seo.to_script,
    static_version=STATIC_VERSION,
)


_footer_cache: dict = {"at": 0.0, "links": []}


def footer_links() -> list[tuple[str, str]]:
    """Links to landing pages that have listings, for crawlers and people. Cached for 5 minutes."""
    if time.time() - _footer_cache["at"] < 300:
        return _footer_cache["links"]
    links = []
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT property_type, deal_type, location, state, count(*) AS n FROM listings "
                "WHERE status = 'published' AND listing_id NOT IN (SELECT listing_id FROM outcomes) "
                "GROUP BY 1, 2, 3, 4 ORDER BY n DESC"
            ).fetchall()
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
    except Exception:  # the footer must never break a page
        log.exception("could not build the footer links")
    _footer_cache.update(at=time.time(), links=links[:40])
    return _footer_cache["links"]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("page", {})
    ctx.update(
        agent_id=security.current_agent_id(request),
        csrf=security.csrf_token(request),
        offline=config.LLM_MODEL == "offline",
        footer=footer_links(),
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
