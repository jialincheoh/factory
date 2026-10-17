"""Evaluate the ad writer against what real estate agents actually did.

Every published listing gives a labelled example for free: the agent corrected
the property card before publishing (ground truth for the card fields) and kept,
edited or replaced a draft (reference for the drafts). This script turns those
records into a dataset and scores a model + prompt on it, so prompt and model
changes can be compared with numbers.

    # 1. Build the dataset from the database (contact details masked, no photos inside)
    python -m scripts.eval_ads export --out eval/ads.jsonl
    python -m scripts.eval_ads export --push factory-ads --confirm-privacy   # also to LangSmith

    # 2. Score the configured LLM_MODEL / prompt on it
    python -m scripts.eval_ads run --data eval/ads.jsonl          # locally, prints a table
    python -m scripts.eval_ads run --langsmith factory-ads        # as a LangSmith experiment

Pushing to LangSmith sends agents' (masked) descriptions to LangSmith, which the
privacy notice does not mention yet: update /privacy, or use only consented or
demo data, before using --push or --langsmith with real agents' listings.
Photos never leave the server; the target reads them from UPLOAD_DIR when present.
"""
import argparse
import json
import re
import statistics
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

from app import config, db
from app.ai import get_assistant, llm, pii

CARD_FIELDS = {  # reference column on listings -> field on the model's PropertyCard
    "property_type": "property_type", "deal_type": "deal_type", "location": "location", "state": "state",
    "price_listed": "price", "land_area_acres": "land_area_acres", "size_sqft": "size_sqft",
    "ceiling_height_ft": "ceiling_height_ft", "power_amps": "power_amps", "power_kva": "power_kva",
    "tenure": "tenure", "zoning": "zoning", "container_access": "container_access", "loading_bays": "loading_bays",
}

EXPORT_SQL = """
SELECT g.request_id, g.model_name, g.prompt_version, l.description_original, d.decision_type, d.final_text,
       l.""" + ", l.".join(CARD_FIELDS) + """,
       coalesce((SELECT array_agg(i.file_name ORDER BY i.position) FROM listing_images i
                 WHERE i.listing_id = l.listing_id), '{}') AS photos
FROM decisions d
JOIN generation_requests g USING (request_id)
JOIN listings l USING (listing_id)
WHERE d.decision_type IN ('kept', 'wrote_own') AND l.status = 'published'
ORDER BY d.decided_at"""


# ------------------------------------------------------------------ dataset

def export_examples(conn) -> list[dict]:
    examples = []
    for r in conn.execute(EXPORT_SQL).fetchall():
        examples.append({
            "inputs": {"description": pii.mask(r["description_original"]), "photos": list(r["photos"]),
                       "n_drafts": config.DRAFTS_PER_REQUEST},
            "outputs": {"card": {k: _plain(r[k]) for k in CARD_FIELDS if r[k] is not None},
                        "final_text": pii.mask(r["final_text"] or ""), "decision": r["decision_type"]},
            "metadata": {"request_id": str(r["request_id"]), "model_name": r["model_name"],
                         "prompt_version": r["prompt_version"]},
        })
    return examples


def _plain(v):
    try:
        return float(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (bool, int)) else v
    except Exception:
        return v


# ------------------------------------------------------------------ target

def target(inputs: dict) -> dict:
    """Run the configured ad writer on one example. Photos are read locally if they still exist."""
    photos = []
    for name in inputs.get("photos") or []:
        path = config.UPLOAD_DIR / name
        if path.exists():
            photos.append((path.read_bytes(), "image/jpeg"))
    started = time.monotonic()
    result = get_assistant().write_ads(inputs["description"], photos, inputs.get("n_drafts", 3))
    return {"card": result.analysis.card.model_dump(), "drafts": [d.model_dump() for d in result.analysis.drafts],
            "checks": result.checks, "model": result.model_name, "photos_used": len(photos),
            "seconds": round(time.monotonic() - started, 2)}


# ------------------------------------------------------------------ evaluators
# LangSmith calls these with keyword arguments; the local runner does the same.

def card_accuracy(outputs: dict, reference_outputs: dict, **_) -> dict:
    """Share of the agent's published card fields the model got right (numbers within 2%)."""
    ref, got = reference_outputs.get("card") or {}, outputs.get("card") or {}
    hits = []
    for column, value in ref.items():
        guess = got.get(CARD_FIELDS[column])
        if isinstance(value, bool) or isinstance(guess, bool):
            hits.append(guess is value)
        elif isinstance(value, (int, float)):
            hits.append(guess is not None and abs(float(guess) - float(value)) <= 0.02 * max(abs(float(value)), 1))
        else:
            hits.append(guess is not None and str(value).strip().lower() in str(guess).strip().lower())
    return {"key": "card_accuracy", "score": sum(hits) / len(hits) if hits else None}


def drafts_supported(outputs: dict, **_) -> dict:
    """Share of drafts the fact check ran on and found nothing unsupported."""
    checks = outputs.get("checks") or []
    ok = [c["checked"] and not c["unsupported_claims"] for c in checks]
    return {"key": "drafts_supported", "score": sum(ok) / len(ok) if ok else None}


def format_ok(outputs: dict, **_) -> dict:
    """Share of drafts within the brief: headline at most 70 characters, body 60 to 120 words."""
    drafts = outputs.get("drafts") or []
    ok = [len(d["headline"]) <= 70 and 60 <= len(d["body"].split()) <= 120 for d in drafts]
    return {"key": "format_ok", "score": sum(ok) / len(ok) if ok else None}


def closeness_to_kept(outputs: dict, reference_outputs: dict, **_) -> dict:
    """How close the best draft is to the text the agent published (1 = identical)."""
    final = reference_outputs.get("final_text") or ""
    drafts = outputs.get("drafts") or []
    if not final or not drafts:
        return {"key": "closeness_to_kept", "score": None}
    return {"key": "closeness_to_kept",
            "score": max(SequenceMatcher(None, f"{d['headline']}\n\n{d['body']}", final).ratio() for d in drafts)}


def no_contact_details(outputs: dict, **_) -> dict:
    """1 if no draft contains a phone number, email or IC number (or a masking placeholder)."""
    text = " ".join(f"{d['headline']} {d['body']}" for d in outputs.get("drafts") or [])
    leaked = pii.mask(text) != text or re.search(r"\[(phone|email|IC number)\]", text)
    return {"key": "no_contact_details", "score": 0.0 if leaked else 1.0}


EVALUATORS = [card_accuracy, drafts_supported, format_ok, closeness_to_kept, no_contact_details]


# ------------------------------------------------------------------ runners

def run_local(examples: list[dict]) -> dict[str, float | None]:
    scores: dict[str, list[float]] = {e.__name__: [] for e in EVALUATORS}
    failed = 0
    for n, ex in enumerate(examples, 1):
        try:
            out = target(ex["inputs"])
        except Exception as exc:  # e.g. quota used up: count it and carry on
            failed += 1
            print(f"[{n}/{len(examples)}] failed: {type(exc).__name__}", file=sys.stderr)
            continue
        row = []
        for ev in EVALUATORS:
            r = ev(inputs=ex["inputs"], outputs=out, reference_outputs=ex["outputs"])
            if r["score"] is not None:
                scores[r["key"]].append(r["score"])
            row.append(f"{r['key']}={'-' if r['score'] is None else round(r['score'], 2)}")
        print(f"[{n}/{len(examples)}] {out['model']} {out['seconds']}s  " + "  ".join(row), file=sys.stderr)
    summary = {k: (round(statistics.mean(v), 3) if v else None) for k, v in scores.items()}
    if failed:
        summary["failed_examples"] = failed
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="build the dataset from the database")
    e.add_argument("--out", type=Path, help="write JSON lines here")
    e.add_argument("--push", metavar="DATASET", help="also create or extend this LangSmith dataset")
    e.add_argument("--confirm-privacy", action="store_true",
                   help="confirm the data may go to LangSmith (see the note at the top of this file)")
    r = sub.add_parser("run", help="score the configured model and prompt")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", type=Path, help="local JSON lines from export")
    src.add_argument("--langsmith", metavar="DATASET", help="run as a LangSmith experiment on this dataset")
    r.add_argument("--limit", type=int)
    args = p.parse_args()

    if args.cmd == "export":
        with db.connect() as conn:
            examples = export_examples(conn)
        print(f"{len(examples)} example(s)", file=sys.stderr)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text("".join(json.dumps(x, default=str) + "\n" for x in examples))
        if args.push:
            if not args.confirm_privacy:
                raise SystemExit("Refusing to push without --confirm-privacy (see the note at the top of this file).")
            from langsmith import Client
            client = Client()
            dataset = (client.read_dataset(dataset_name=args.push) if client.has_dataset(dataset_name=args.push)
                       else client.create_dataset(args.push, description="Factory ad writer: agent decisions"))
            client.create_examples(dataset_id=dataset.id, examples=examples)
            print(f"pushed to LangSmith dataset {args.push}", file=sys.stderr)
        return

    experiment = f"{config.LLM_MODEL}-{llm.PROMPT_VERSION}".replace(":", "-")
    if args.langsmith:
        from langsmith import evaluate
        results = evaluate(target, data=args.langsmith, evaluators=EVALUATORS, experiment_prefix=experiment,
                           max_concurrency=2, metadata={"model": config.LLM_MODEL, "prompt": llm.PROMPT_VERSION})
        print(f"LangSmith experiment: {results.experiment_name}")
        return
    examples = [json.loads(line) for line in args.data.read_text().splitlines() if line.strip()][:args.limit]
    summary = run_local(examples)
    print(json.dumps({"experiment": experiment, "examples": len(examples), **summary}, indent=2))


if __name__ == "__main__":
    main()
