"""Application wiring. The buyer portal is at /, the real estate agent tools at /agent.

Routes live in app/routes, business logic in app/services, the language-model code in app/ai.
"""

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .routes import agent_account, agent_ads, portal, search_api, seo_pages
from .services import market, video
from .web.middleware import security_headers
from .web.templating import STATIC_DIR, render


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stops = []
    if config.VIDEO_WORKER:
        stops.append(video.start_worker())
    if config.MARKET_CHECK_WORKER:
        stops.append(market.start_worker())
    yield
    for stop in stops:
        stop.set()


app = FastAPI(title=config.SITE_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or secrets.token_hex(32),  # random = sessions reset on restart
    session_cookie="session",
    same_site="lax",
    https_only=config.SECURE_COOKIES,
    max_age=8 * 3600,
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.middleware("http")(security_headers)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(portal.router)
app.include_router(search_api.router)
app.include_router(agent_account.router)
app.include_router(agent_ads.router)
app.include_router(seo_pages.router)  # last: its catch-all paths must not shadow the others


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return render(
        request,
        "portal/message.html",
        404,
        heading="Page not found",
        text="This listing may have been taken down, or the link is mistyped.",
    )


@app.exception_handler(410)
async def gone(request: Request, exc):
    return render(
        request,
        "portal/message.html",
        410,
        heading="This property is no longer available",
        text="It has been sold, rented out or withdrawn. Similar properties may still be listed.",
    )
