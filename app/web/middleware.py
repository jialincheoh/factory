"""Response headers applied to every page."""

from fastapi import Request

from .templating import MAP_TILES, STATIC_VERSION


async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; img-src 'self' data: {MAP_TILES}; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    path = request.url.path
    if path.startswith("/static/") and request.query_params.get("v") == STATIC_VERSION:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith(("/uploads/", "/videos/")) and response.status_code in (200, 206):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # random, never-reused names
    if path.startswith(("/agent", "/api/")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    # OpenStreetMap refuses tiles without a Referer; send only our origin, never the page path.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
