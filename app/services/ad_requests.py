"""Storing what the ad writer produced and what the real estate agent decided.

Every draft is kept, including rejected ones, in a seeded random order: the logged seed and
display position separate "agents keep the best draft" from "agents keep whatever comes first".
"""

import random
import time
import uuid

from psycopg.types.json import Jsonb

from .. import config, db
from ..ai import get_assistant
from ..ai.calllog import CallRecorder
from ..ai.llm import PROMPT_VERSION
from . import images, revisions
from .tracking import log_model_calls

EXPERIMENT_ARM = "random_order_v1"  # drafts shown in seeded random order


def create_request(
    conn, agent_id, listing_id, description: str, photos: list[bytes], recorder: CallRecorder
) -> uuid.UUID:
    """Call the model, then store the request, every draft in shuffled order, and the model calls."""
    started = time.monotonic()
    result = get_assistant().write_ads(
        description, [(p, "image/jpeg") for p in photos], config.DRAFTS_PER_REQUEST, recorder
    )
    return store_request(conn, agent_id, listing_id, result, int((time.monotonic() - started) * 1000), recorder)


def store_request(conn, agent_id, listing_id, result, latency_ms: int, recorder: CallRecorder) -> uuid.UUID:
    analysis = result.analysis
    request_id, seed = uuid.uuid4(), random.SystemRandom().getrandbits(63)
    conn.execute(
        "INSERT INTO generation_requests (request_id, listing_id, real_estate_agent_id, model_name, "
        "prompt_version, prompt_text, experiment_arm, order_seed, analysis) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            request_id,
            listing_id,
            agent_id,
            result.model_name,
            PROMPT_VERSION,
            result.prompt_text,
            EXPERIMENT_ARM,
            seed,
            Jsonb(analysis.model_dump(exclude={"drafts"})),
        ],
    )

    order = list(range(len(analysis.drafts)))
    random.Random(seed).shuffle(order)  # reproducible from the logged seed
    raw = analysis.model_dump()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO candidates (candidate_id, request_id, display_position, headline, ad_text, "
            "raw_response, latency_ms, cover_position) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    uuid.uuid4(),
                    request_id,
                    pos,
                    analysis.drafts[i].headline,
                    analysis.drafts[i].body,
                    Jsonb({"generated_index": i, "analysis": raw, "check": result.checks[i]}),
                    latency_ms,
                    analysis.drafts[i].cover_photo,
                )
                for pos, i in enumerate(order, 1)
            ],
        )
    log_model_calls(conn, "write_ads", recorder, request_id=request_id)
    return request_id


def save_new_listing(
    agent_id, description: str, processed: list, result, latency_ms: int, recorder: CallRecorder
) -> uuid.UUID:
    """Listing, photos and the generation request in one transaction, after the model has answered."""
    with db.connect() as conn:
        listing_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO listings (listing_id, real_estate_agent_id, description_original) VALUES (%s, %s, %s)",
            [listing_id, agent_id, description],
        )
        for pos, (data, w, h) in enumerate(processed, 1):
            conn.execute(
                "INSERT INTO listing_images (image_id, listing_id, position, file_name, width, height) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                [uuid.uuid4(), listing_id, pos, images.save(data, conn), w, h],
            )
        return store_request(conn, agent_id, listing_id, result, latency_ms, recorder)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance in characters."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def publish_ad(
    conn,
    request_id,
    req: dict,
    candidate: dict | None,
    decision_type: str,
    headline: str,
    body: str,
    card: dict,
    ranks: dict,
    cover: int,
    seconds_to_decide: int | None,
) -> None:
    """Record the agent's decision and put the ad live, in the caller's transaction."""
    final_text = f"{headline}\n\n{body}"
    if candidate:  # the agent edited the latest revision of the draft, if they asked for any
        shown = revisions.current_drafts(conn, request_id)
        shown = next(d for d in shown.values() if d["candidate_id"] == candidate["candidate_id"])
        candidate = {**candidate, "headline": shown["headline"], "ad_text": shown["ad_text"]}
    conn.execute(
        "INSERT INTO decisions (request_id, decision_type, kept_candidate_id, final_text, edit_distance, "
        "decided_at, seconds_to_decide, cover_position) VALUES (%s,%s,%s,%s,%s,now(),%s,%s)",
        [
            request_id,
            decision_type,
            candidate and candidate["candidate_id"],
            final_text,
            edit_distance(f"{candidate['headline']}\n\n{candidate['ad_text']}", final_text) if candidate else None,
            seconds_to_decide,
            cover,
        ],
    )
    with conn.cursor() as cur:  # captions from this request become the photos' alt text
        cur.executemany(
            "UPDATE listing_images SET caption = %s WHERE listing_id = %s AND position = %s",
            [
                (c["caption"][:200], req["listing_id"], c["photo"])
                for c in (req["analysis"] or {}).get("photo_captions") or []
            ],
        )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO rankings (request_id, candidate_id, rank) VALUES (%s,%s,%s)",
            [(request_id, c, r) for c, r in ranks.items()],
        )
    card["description_language"] = (req["analysis"] or {}).get("detected_language")
    conn.execute(
        "UPDATE listings SET status = 'published', updated_at = now(), cover_position = %s, "
        + ", ".join(f"{col} = %s" for col in card)
        + " WHERE listing_id = %s",
        [cover, *card.values(), req["listing_id"]],
    )  # column names come from card_from_form, never from the request
    conn.execute(
        "INSERT INTO published_ads (ad_id, request_id, listing_id, channel, published_at, headline, ad_text) "
        "VALUES (%s,%s,%s,'own_portal',now(),%s,%s)",
        [uuid.uuid4(), request_id, req["listing_id"], headline, body],
    )
