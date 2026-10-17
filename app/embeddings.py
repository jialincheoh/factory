"""Meaning-based search: each published ad gets an embedding (LangChain's
init_embeddings, so the provider is a config value, like the chat model), stored
in Postgres with pgvector. When a buyer describes a use rather than specs
("suitable for food processing", "cold storage"), the search loop passes that
as semantic_query and results are ranked by similarity; hard filters still apply.

Ad text is already public, so embedding it sends nothing private. Buyers' words
go to the same provider as their chat message, after the same contact masking.
"""
import json
import logging
from functools import lru_cache

from . import config

DIMENSIONS = 768  # must match published_ads.embedding vector(768)
log = logging.getLogger("factory.embeddings")


@lru_cache(maxsize=1)
def embedder():
    """The configured embeddings model, or None when meaning-based search is off."""
    spec = config.EMBEDDINGS_MODEL
    if not spec:
        return None
    if spec == "fake":  # tests: deterministic vectors, no API
        return _HashEmbeddings()
    from langchain.embeddings import init_embeddings
    kwargs = {"output_dimensionality": DIMENSIONS} if spec.startswith("google_genai:") else {"dimensions": DIMENSIONS}
    return init_embeddings(spec, **kwargs)


class _HashEmbeddings:
    """Deterministic stand-in with LangChain's Embeddings interface (same text, same vector), so tests
    need neither an API key nor numpy (which langchain_core's DeterministicFakeEmbedding requires)."""

    def _vec(self, text: str) -> list[float]:
        import hashlib
        import random
        rng = random.Random(hashlib.sha256(text.encode()).digest())
        return [rng.gauss(0, 1) for _ in range(DIMENSIONS)]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


@lru_cache(maxsize=512)
def query_vector(text: str) -> str | None:
    """pgvector literal for a buyer's words, or None if embeddings are off or the call fails."""
    model = embedder()
    if model is None or not text.strip():
        return None
    try:
        return vector_literal(model.embed_query(text))
    except Exception as exc:
        log.warning(json.dumps({"embed_query_failed": type(exc).__name__}))
        return None


def ad_text(row: dict) -> str:
    features = ", ".join(row.get("features") or [])
    return "\n".join(x for x in [row.get("title"), row.get("headline"), row.get("ad_text"),
                                 f"Location: {row.get('location') or ''} {row.get('state') or ''}".strip(),
                                 f"Features: {features}" if features else None] if x)


def embed_missing(conn, limit: int = 100) -> int:
    """Embed live ads that have no embedding (or one from another model). Returns how many were embedded.
    Never raises: search simply falls back to newest-first for ads without one."""
    model = embedder()
    if model is None:
        return 0
    try:
        rows = conn.execute("""
            SELECT pa.ad_id, pa.headline, pa.ad_text, l.title, l.location, l.state, l.features
            FROM published_ads pa JOIN listings l USING (listing_id)
            WHERE l.status = 'published' AND (pa.embedding IS NULL OR pa.embedding_model IS DISTINCT FROM %s)
            ORDER BY pa.published_at DESC LIMIT %s""", [config.EMBEDDINGS_MODEL, limit]).fetchall()
        if not rows:
            return 0
        vectors = model.embed_documents([ad_text(r) for r in rows])
        with conn.cursor() as cur:
            cur.executemany("UPDATE published_ads SET embedding = %s::vector, embedding_model = %s WHERE ad_id = %s",
                            [(vector_literal(v), config.EMBEDDINGS_MODEL, r["ad_id"]) for r, v in zip(rows, vectors)])
        conn.commit()
        return len(rows)
    except Exception as exc:
        conn.rollback()
        log.warning(json.dumps({"embed_missing_failed": type(exc).__name__, "detail": str(exc)[:200]}))
        return 0


if __name__ == "__main__":  # python -m app.embeddings: backfill, e.g. after seeding or a model change
    from . import db
    with db.connect() as conn:
        total = 0
        while (n := embed_missing(conn)):
            total += n
        print(f"embedded {total} ad(s)")
