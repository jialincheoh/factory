"""The buyer filter bar: form fields to SearchFilters and back, and the JSON the page updates itself from.

The filter bar and the chat share one SearchFilters object: the bar is sent with every chat
message, and the chat's filters are written back to the bar.
"""

from .. import geo, seo
from ..ai.schemas import PROPERTY_TYPES, SearchFilters
from .templating import number, price_label, templates


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
    out = {
        "deal": f.deal_type or "",
        "type": t,
        "area": f.location or "",
        "state": f.state or "",
        "max_price": f.max_price,
        "min_acres": f.min_land_acres,
        "min_power": f.min_power_amps,
        "min_ceiling": f.min_ceiling_ft,
        "tenure": f.tenure or "",
        "container": bool(f.container_access),
    }
    return {k: ("" if v is None else (number(v, 2) if isinstance(v, float) else v)) for k, v in out.items()}


def results_payload(results: list[dict]) -> dict:
    return {
        "count": len(results),
        "html": templates.get_template("portal/_cards.html").render(listings=results),
        "markers": [
            {
                "id": str(r["listing_id"]),
                "url": seo.listing_path(r),
                "lat": r["latitude"],
                "lng": r["longitude"],
                "title": r["title"],
                "price": price_label(r),
            }
            for r in results
            if r["latitude"] is not None
        ],
    }
