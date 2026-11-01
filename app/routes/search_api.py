"""JSON endpoints behind the buyer search box: filters, chat, and voice or photo messages."""

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import config, db, security
from ..ai import get_assistant, pii
from ..ai.calllog import CallRecorder
from ..ai.schemas import SearchFilters
from ..services import images
from ..services.tracking import log_events, log_failed_calls, log_model_calls, log_search, visitor_id
from ..web.search_forms import filters_from_form, form_from_filters, results_payload

log = logging.getLogger("factory.search")
router = APIRouter()


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415)  # JSON-only, so cross-site forms cannot post here
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400)
    return body


@router.post("/api/search")
async def search(request: Request):
    filters = filters_from_form(await _json_body(request))
    vid = visitor_id(request)

    def run():
        with db.connect() as conn:
            results = db.search_listings(conn, filters)
            log_events(conn, [r["ad_id"] for r in results], "impression", vid)
            log_search(conn, vid, "filters", filters, len(results))
            return results

    return results_payload(await run_in_threadpool(run))


MAX_AUDIO_BYTES = 3 * 1024 * 1024  # 30 s of 16 kHz 16-bit mono WAV is about 1 MB
AUDIO_TYPES = {
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp4",
    "audio/aac",
    "audio/ogg",
    "audio/flac",
    "audio/webm",
}


def _history(raw) -> list[dict]:
    history = [
        {"role": "user" if m.get("role") == "user" else "assistant", "content": str(m.get("content", ""))[:1000]}
        for m in (raw if isinstance(raw, list) else [])[-12:]
        if isinstance(m, dict)
    ]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "Send a message.")
    return history


async def _answer_chat(
    request: Request,
    history: list[dict],
    current: SearchFilters,
    method: str,
    recorder: CallRecorder,
    image: tuple[bytes, str] | None = None,
    extra: dict | None = None,
):
    """Run the search loop for the newest buyer turn, log the search and the model calls, and shape the reply."""
    vid, typed = visitor_id(request), history[-1]["content"]

    def run():
        with db.connect() as conn:
            res = get_assistant().search_chat(
                history, lambda f: db.search_listings(conn, f), current, recorder, image=image
            )
            if res.results is not None:
                log_events(conn, [r["ad_id"] for r in res.results], "impression", vid)
            search_id = log_search(
                conn,
                vid,
                method,
                res.filters or current,
                len(res.results) if res.results is not None else None,
                typed,
                res.model_name,
                res.declined,
            )
            log_model_calls(conn, "search_chat", recorder, search_id=search_id)
            return res

    try:
        res = await run_in_threadpool(run)
    except Exception:
        try:  # record the failed search too, so failure rates can be measured
            with db.connect() as conn:
                search_id = log_search(conn, vid, method, current, None, typed, "error")
                log_model_calls(conn, "search_chat", recorder, search_id=search_id)
        except Exception:
            log.exception("could not record a failed search")
        return JSONResponse({"reply": "Sorry, search is unavailable right now. Please try again."}, status_code=502)

    out = {"reply": res.reply, **(extra or {})}
    if res.results is not None:
        out.update(results_payload(res.results), form=form_from_filters(res.filters))
    return out


def _chat_allowed(request: Request) -> bool:
    ip = request.client.host if request.client else "unknown"
    return security.CHAT_PER_VISITOR.allow(visitor_id(request)) and security.CHAT_PER_IP.allow(ip)


BUSY_REPLY = {
    "reply": "You are sending messages faster than we can answer. Please wait a few minutes, or use the filters."
}


@router.post("/api/chat")
async def chat(request: Request):
    body = await _json_body(request)
    history = _history(body.get("messages"))
    if not history[-1]["content"].strip():
        raise HTTPException(400, "Send a message.")
    current = filters_from_form(body.get("filters") or {})
    if not _chat_allowed(request):
        return JSONResponse(BUSY_REPLY, status_code=429)
    return await _answer_chat(request, history, current, "text", CallRecorder())


@router.post("/api/chat/media")
async def chat_media(
    request: Request,
    messages: str = Form("[]"),
    filters: str = Form("{}"),
    audio: UploadFile | None = File(None),
    photo: UploadFile | None = File(None),
):
    """A voice message or a photo from the search box. The clip is transcribed first, so the words go
    through the same masking, logging and search loop as typed text; a photo is shown to the model
    with the newest message. Neither file is kept."""
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(415)  # same protection as the JSON-only endpoint: plain cross-site forms cannot post here
    try:
        body = {"messages": json.loads(messages), "filters": json.loads(filters)}
    except ValueError as exc:
        raise HTTPException(400, "Bad request.") from exc
    history = _history(body["messages"])
    current = filters_from_form(body["filters"] if isinstance(body["filters"], dict) else {})
    if not _chat_allowed(request):
        return JSONResponse(BUSY_REPLY, status_code=429)
    recorder = CallRecorder()

    if audio is not None and audio.filename:
        mime = (audio.content_type or "").split(";")[0]
        if mime not in AUDIO_TYPES:
            raise HTTPException(400, "Unsupported audio format.")
        clip = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(clip) > MAX_AUDIO_BYTES:
            raise HTTPException(400, "Voice messages are limited to 30 seconds.")
        try:
            transcript = await run_in_threadpool(get_assistant().transcribe, clip, mime, recorder)
        except Exception:
            transcript = ""
        if not transcript:
            log_failed_calls("transcribe", recorder)
            return JSONResponse(
                {
                    "reply": "I could not make out any words in that recording. Please try again "
                    "or type what you need.",
                    "heard": "",
                },
                status_code=200,
            )
        history[-1]["content"] = transcript[:1000]
        return await _answer_chat(request, history, current, "voice", recorder, extra={"heard": pii.mask(transcript)})

    if photo is not None and photo.filename:
        try:
            jpeg, _, _ = images.process_upload(await photo.read(config.MAX_IMAGE_BYTES + 1))
        except images.ImageError as exc:
            raise HTTPException(400, str(exc)) from exc
        caption = history[-1]["content"].strip()
        history[-1]["content"] = f"[photo] {caption}".strip()
        return await _answer_chat(request, history, current, "image", recorder, image=(jpeg, "image/jpeg"))

    raise HTTPException(400, "Attach a voice message or a photo.")
