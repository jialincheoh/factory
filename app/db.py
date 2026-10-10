"""Postgres access. One short-lived connection per request; every query is
parameterised (psycopg sends values separately from the SQL text)."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config
from .ai.schemas import LANDED_TYPES, SearchFilters


@contextmanager
def connect():
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        yield conn  # commits on clean exit, rolls back on exception


def get_conn():
    """FastAPI dependency."""
    with connect() as conn:
        yield conn


LISTING_CARD_SQL = """
SELECT l.listing_id, l.title, l.property_type, l.deal_type, l.location, l.state, l.bedrooms, l.bathrooms,
       l.size_sqft, l.price_listed AS price, l.features, l.land_area_acres, l.ceiling_height_ft,
       l.power_amps, l.power_kva, l.power_amps_equiv, l.tenure, l.lease_years_left, l.zoning,
       l.container_access, l.loading_bays, l.latitude, l.longitude,
       pa.ad_id, pa.headline, pa.ad_text, pa.published_at,
       (SELECT file_name FROM listing_images i
         WHERE i.listing_id = l.listing_id ORDER BY position LIMIT 1) AS cover
FROM listings l
JOIN LATERAL (
  SELECT * FROM published_ads p WHERE p.listing_id = l.listing_id
  ORDER BY published_at DESC LIMIT 1
) pa ON true
WHERE l.status = 'published'
"""


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_listings(conn, f: SearchFilters, limit: int = 60) -> list[dict]:
    where, params = [], []

    def add(sql, *values):
        where.append(sql); params.extend(values)

    if f.deal_type:
        add("l.deal_type = %s", f.deal_type)
    if f.property_types:
        types = set(f.property_types)
        if "landed" in types:  # "a house" covers every landed type
            types |= set(LANDED_TYPES)
        add("l.property_type = ANY(%s)", sorted(types))
    if f.location:
        pat = f"%{_escape_like(f.location)}%"
        add("(l.location ILIKE %s OR l.state ILIKE %s)", pat, pat)
    if f.state:
        add("l.state ILIKE %s", _escape_like(f.state))
    if f.min_price is not None:
        add("l.price_listed >= %s", f.min_price)
    if f.max_price is not None:
        add("l.price_listed <= %s", f.max_price)
    if f.min_land_acres is not None:
        add("l.land_area_acres >= %s", f.min_land_acres)
    if f.max_land_acres is not None:
        add("l.land_area_acres <= %s", f.max_land_acres)
    if f.min_built_up_sqft is not None:
        add("l.size_sqft >= %s", f.min_built_up_sqft)
    if f.min_ceiling_ft is not None:
        add("l.ceiling_height_ft >= %s", f.min_ceiling_ft)
    if f.min_power_amps is not None:
        add("l.power_amps_equiv >= %s", f.min_power_amps)  # amps, or kVA converted to amps
    if f.tenure:
        add("l.tenure = %s", f.tenure)
    if f.zoning:
        add("l.zoning = ANY(%s)", list(f.zoning))
    if f.container_access:
        add("l.container_access")
    if f.min_loading_bays is not None:
        add("l.loading_bays >= %s", f.min_loading_bays)
    if f.min_bedrooms is not None:
        add("l.bedrooms >= %s", f.min_bedrooms)
    for feat in f.features:
        # A feature counts if it is tagged on the card or mentioned in the ad.
        pat = f"%{_escape_like(feat.lower().strip())}%"
        add("(EXISTS (SELECT 1 FROM unnest(l.features) t WHERE t ILIKE %s) OR pa.ad_text ILIKE %s)", pat, pat)
    if f.keywords:
        pat = f"%{_escape_like(f.keywords)}%"
        add("(pa.ad_text ILIKE %s OR pa.headline ILIKE %s OR l.title ILIKE %s)", pat, pat, pat)

    sql = LISTING_CARD_SQL + "".join(f" AND {w}" for w in where) + " ORDER BY pa.published_at DESC LIMIT %s"
    return conn.execute(sql, [*params, limit]).fetchall()
