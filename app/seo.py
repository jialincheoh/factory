"""Search-engine helpers: readable URLs, landing pages for the searches
people type into Google ("factory for sale shah alam"), and schema.org data.
"""
import json
import re

from . import config, geo
from .ai.schemas import LAND_TYPES

# URL path -> (property types, deal type, plural heading, Malay phrase)
LANDING = {
    "factory-for-sale": (["factory"], "sale", "Factories for sale", "Kilang untuk dijual"),
    "factory-for-rent": (["factory"], "rent", "Factories for rent", "Kilang untuk disewa"),
    "warehouse-for-sale": (["warehouse"], "sale", "Warehouses for sale", "Gudang untuk dijual"),
    "warehouse-for-rent": (["warehouse"], "rent", "Warehouses for rent", "Gudang untuk disewa"),
    "land-for-sale": (list(LAND_TYPES), "sale", "Land for sale", "Tanah untuk dijual"),
    "industrial-land-for-sale": (["industrial_land"], "sale", "Industrial land for sale", "Tanah industri untuk dijual"),
    "commercial-land-for-sale": (["commercial_land"], "sale", "Commercial land for sale", "Tanah komersial untuk dijual"),
    "agricultural-land-for-sale": (["agricultural_land"], "sale", "Agricultural land for sale", "Tanah pertanian untuk dijual"),
}


def slugify(text: str | None, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].rstrip("-") or "listing"


PLACES = {slugify(name): ("area", name) for name in geo.AREAS} | {slugify(s): ("state", s) for s in geo.STATES}


def place_from_slug(slug: str):
    """('area'|'state', proper name) or None."""
    return PLACES.get(slug)


def landing_path(listing: dict) -> str | None:
    """Most specific landing page a listing belongs to (for breadcrumbs)."""
    for path, (types, deal, *_rest) in LANDING.items():
        if types == [listing.get("property_type")] and deal == listing.get("deal_type"):
            return path
    if listing.get("property_type") in LAND_TYPES and listing.get("deal_type") == "sale":
        return "land-for-sale"
    return None


def listing_path(listing: dict) -> str:
    return f"/listings/{listing['listing_id']}/{slugify(listing.get('title'))}"


def absolute(path: str) -> str:
    return config.SITE_URL.rstrip("/") + path


def _clean(obj):
    """Drop empty values so the JSON-LD has no nulls."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if v not in (None, "", [], {})}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def to_script(data) -> str:
    """JSON-LD safe to place inside <script type="application/ld+json">."""
    return json.dumps(_clean(data), ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def breadcrumbs(items: list[tuple[str, str]]) -> dict:
    """items: [(name, path), ...] from the home page down."""
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i, "name": name, "item": absolute(path)}
                                for i, (name, path) in enumerate(items, 1)]}


def listing_jsonld(listing: dict, photos: list[str]) -> dict:
    sqft = listing.get("size_sqft")
    land_sqft = round(float(listing["land_area_acres"]) * 43560) if listing.get("land_area_acres") else None
    return {
        "@context": "https://schema.org",
        "@type": "RealEstateListing",
        "name": listing.get("headline") or listing.get("title"),
        "description": (listing.get("ad_text") or "")[:500],
        "url": absolute(listing_path(listing)),
        "datePosted": listing["published_at"].date().isoformat() if listing.get("published_at") else None,
        "image": [absolute(f"/uploads/{p}") for p in photos],
        "offers": {
            "@type": "Offer",
            "price": float(listing["price"]) if listing.get("price") else None,
            "priceCurrency": config.CURRENCY_CODE,
            "businessFunction": "http://purl.org/goodrelations/v1#" + ("LeaseOut" if listing.get("deal_type") == "rent" else "Sell"),
            "availability": "https://schema.org/InStock",
        },
        "about": {
            "@type": "Place",
            "name": listing.get("title"),
            "address": {"@type": "PostalAddress", "addressLocality": listing.get("location"),
                        "addressRegion": listing.get("state"), "addressCountry": "MY"},
            "geo": {"@type": "GeoCoordinates", "latitude": listing.get("latitude"),
                    "longitude": listing.get("longitude")} if listing.get("latitude") is not None else None,
            "additionalProperty": [
                {"@type": "PropertyValue", "name": name, "value": value, "unitText": unit}
                for name, value, unit in (
                    ("Built-up area", sqft, "sqft"),
                    ("Land area", land_sqft, "sqft"),
                    ("Power supply", listing.get("power_amps") or listing.get("power_kva"),
                     "A" if listing.get("power_amps") else "kVA"),
                    ("Ceiling height", float(listing["ceiling_height_ft"]) if listing.get("ceiling_height_ft") else None, "ft"),
                    ("Tenure", listing.get("tenure"), None),
                    ("Container access", "yes" if listing.get("container_access") else None, None),
                ) if value is not None
            ],
        },
    }


def site_jsonld() -> dict:
    return {"@context": "https://schema.org", "@type": "WebSite", "name": config.SITE_NAME, "url": absolute("/"),
            "inLanguage": ["en-MY", "ms-MY"]}


def organization_jsonld() -> dict:
    return {"@context": "https://schema.org", "@type": "Organization", "name": config.SITE_NAME,
            "url": absolute("/"), "logo": absolute("/static/icon-512.png")}
