"""Validation of the review page's form: the property card and the optional creativity ranking."""

from fastapi import HTTPException

from .. import geo
from ..ai.schemas import PROPERTY_TYPES, ZONINGS


def opt_int(value: str | None, field: str) -> int | None:
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
    except ValueError as exc:
        raise HTTPException(400, f"{field} must be a number.") from exc
    if not lo <= v <= hi:
        raise HTTPException(400, f"{field} is out of range.")
    return v


def card_from_form(form) -> dict:
    """Validated listing columns from the review form, keyed by column name."""

    def text(key: str, limit: int = 120) -> str | None:
        return str(form.get(key, "")).strip()[:limit] or None

    property_type = form.get("property_type")
    if property_type not in PROPERTY_TYPES:
        raise HTTPException(
            400,
            "This site lists factories, warehouses, industrial or commercial land and whole commercial buildings only. "
            "Shoplots, shop offices and office units belong on kedai.my.",
        )
    tenure = form.get("tenure") if form.get("tenure") in ("freehold", "leasehold") else None
    lat, lng = (
        _opt_float(form.get("latitude"), "Latitude", -90, 90),
        _opt_float(form.get("longitude"), "Longitude", -180, 180),
    )
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
        "size_sqft": opt_int(form.get("size_sqft"), "Built-up area") or None,
        "ceiling_height_ft": _opt_float(form.get("ceiling_height_ft"), "Ceiling height", 1, 999),
        "power_amps": opt_int(form.get("power_amps"), "Power (amps)") or None,
        "power_kva": opt_int(form.get("power_kva"), "Power (kVA)") or None,
        "tenure": tenure,
        "lease_years_left": (opt_int(form.get("lease_years_left"), "Lease years left") or None)
        if tenure == "leasehold"
        else None,
        "zoning": form.get("zoning") if form.get("zoning") in ZONINGS else None,
        "container_access": {"yes": True, "no": False}.get(form.get("container_access")),
        "loading_bays": opt_int(form.get("loading_bays"), "Loading bays"),
        "features": sorted({f.strip().lower()[:40] for f in str(form.get("features", "")).split(",") if f.strip()})[
            :30
        ],
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
    except ValueError as exc:
        raise HTTPException(400, "Rank every draft, or leave the ranking empty.") from exc
    if sorted(ranks.values()) != list(range(1, len(candidate_ids) + 1)):
        raise HTTPException(400, "Give each draft a different rank, from 1 (most creative) down.")
    return ranks
