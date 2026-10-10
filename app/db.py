"""Postgres access. One short-lived connection per request; every query is
parameterised (psycopg sends values separately from the SQL text)."""
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config
from .ai.schemas import SearchFilters


@contextmanager
def connect():
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        yield conn  # commits on clean exit, rolls back on exception


def get_conn():
    """FastAPI dependency."""
    with connect() as conn:
        yield conn


LISTING_CARD_SQL = """
SELECT l.listing_id, l.title, l.property_type, l.location, l.bedrooms, l.bathrooms,
       l.size_sqft, l.price_listed AS price, l.features,
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


LANDED_TYPES = ["landed", "bungalow", "townhouse"]


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_listings(conn, f: SearchFilters, limit: int = 50) -> list[dict]:
    where, params = [], []
    if f.property_type:
        # "landed" (a house) covers every landed type.
        types = LANDED_TYPES if f.property_type == "landed" else [f.property_type]
        where.append("l.property_type = ANY(%s)"); params.append(types)
    if f.min_bedrooms is not None:
        where.append("l.bedrooms >= %s"); params.append(f.min_bedrooms)
    if f.max_bedrooms is not None:
        where.append("l.bedrooms <= %s"); params.append(f.max_bedrooms)
    if f.min_bathrooms is not None:
        where.append("l.bathrooms >= %s"); params.append(f.min_bathrooms)
    if f.min_price is not None:
        where.append("l.price_listed >= %s"); params.append(f.min_price)
    if f.max_price is not None:
        where.append("l.price_listed <= %s"); params.append(f.max_price)
    if f.location:
        where.append("l.location ILIKE %s"); params.append(f"%{_escape_like(f.location)}%")
    for feat in f.features:
        # A feature counts if it is tagged on the card or mentioned in the ad.
        pat = f"%{_escape_like(feat.lower().strip())}%"
        where.append("(EXISTS (SELECT 1 FROM unnest(l.features) t WHERE t ILIKE %s) OR pa.ad_text ILIKE %s)")
        params += [pat, pat]
    if f.keywords:
        pat = f"%{_escape_like(f.keywords)}%"
        where.append("(pa.ad_text ILIKE %s OR pa.headline ILIKE %s OR l.title ILIKE %s)")
        params += [pat, pat, pat]

    sql = LISTING_CARD_SQL + "".join(f" AND {w}" for w in where) + " ORDER BY pa.published_at DESC LIMIT %s"
    return conn.execute(sql, [*params, limit]).fetchall()
