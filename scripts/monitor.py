"""Production health report from the database, with alerts.

    python -m scripts.monitor            # last 7 days
    python -m scripts.monitor --days 1

Exits with status 1 when any alert fires, so a scheduled job (e.g. Cloud
Scheduler running it as a Cloud Run job) can notify on failure.

What it watches:
  * model calls: failure rate (including replies that were not valid
    structured output), fallback rate, latency, tokens per request
  * silent changes: the provider reporting a different model name for the same
    configured model, or a prompt edited without a new prompt version
  * draft check: share of drafts flagged or left unchecked
  * search: failed, declined and zero-result searches, and per visit whether a
    typed search led to opening a listing or revealing a phone number
"""
import argparse
import sys

import psycopg
from psycopg.rows import dict_row

from app import config

# Alert thresholds. First guesses: tune once there is real traffic.
MAX_FAILURE_RATE = 0.05
MAX_FALLBACK_RATE = 0.20
MAX_P95_MS = {"write_ads": 60_000, "search_chat": 15_000}  # per request
MAX_UNCHECKED_RATE = 0.10
MAX_SEARCH_ERROR_RATE = 0.05

CALLS = """
SELECT job, count(*) AS calls,
       avg((NOT ok)::int) AS failure_rate,
       count(*) FILTER (WHERE error = 'InvalidStructuredOutput') AS format_failures,
       avg(is_fallback::int) AS fallback_rate,
       sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens
FROM model_calls WHERE occurred_at > now() - make_interval(days => %(days)s) GROUP BY job ORDER BY job"""

# Latency per request: the sum of its model calls.
REQUEST_LATENCY = """
SELECT job, count(*) AS requests,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95_ms
FROM (SELECT job, coalesce(request_id::text, search_id::text, call_id::text) AS req, sum(latency_ms) AS ms
      FROM model_calls WHERE occurred_at > now() - make_interval(days => %(days)s) GROUP BY job, req) r
GROUP BY job ORDER BY job"""

# A model name the provider reports for the first time, per configured model.
NEW_SERVED_BY = """
SELECT model, served_by, min(occurred_at) AS first_seen FROM model_calls
WHERE ok AND served_by IS NOT NULL GROUP BY model, served_by
HAVING min(occurred_at) > now() - make_interval(days => %(days)s)
   AND EXISTS (SELECT 1 FROM model_calls o WHERE o.model = model_calls.model AND o.served_by IS NOT NULL
               AND o.occurred_at < now() - make_interval(days => %(days)s))"""

PROMPT_EDITS = """
SELECT prompt_version, array_agg(DISTINCT prompt_hash) AS hashes FROM model_calls
GROUP BY prompt_version HAVING count(DISTINCT prompt_hash) > 1"""

DRAFT_CHECK = """
SELECT count(*) AS drafts,
       avg(((c.raw_response->'check'->>'checked')::boolean IS NOT TRUE)::int) AS unchecked_rate,
       avg((jsonb_array_length(coalesce(c.raw_response->'check'->'unsupported_claims', '[]')) > 0)::int) AS flagged_rate,
       avg((jsonb_array_length(coalesce(c.raw_response->'check'->'earlier_versions', '[]')) > 0)::int) AS rewritten_rate
FROM candidates c JOIN generation_requests g USING (request_id)
WHERE g.created_at > now() - make_interval(days => %(days)s) AND g.model_name <> 'offline-rules'"""

SEARCHES = """
SELECT count(*) AS typed,
       avg((interpreter = 'error')::int) AS error_rate,
       avg((declined IS NOT NULL)::int) AS declined_rate,
       avg((result_count = 0)::int) AS zero_result_rate
FROM search_events WHERE method IN ('text', 'voice', 'image') AND occurred_at > now() - make_interval(days => %(days)s)"""

# Task level: did a visit that typed, spoke or photographed a search go on to open a listing or reveal a phone number,
# and how many typed messages did it take to reach the first opened listing?
SEARCH_TASKS = """
WITH visits AS (
  SELECT visitor_id, min(occurred_at) AS first_typed FROM search_events
  WHERE method IN ('text', 'voice', 'image') AND occurred_at > now() - make_interval(days => %(days)s) GROUP BY visitor_id),
outcome AS (
  SELECT v.visitor_id,
         min(e.occurred_at) FILTER (WHERE e.event_type = 'click') AS first_open,
         bool_or(e.event_type = 'phone_reveal') AS revealed
  FROM visits v LEFT JOIN ad_events e ON e.visitor_id = v.visitor_id AND e.occurred_at >= v.first_typed
  GROUP BY v.visitor_id)
SELECT count(*) AS visits,
       avg((first_open IS NOT NULL)::int) AS opened_rate,
       avg(coalesce(revealed, false)::int) AS revealed_rate,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (
         SELECT count(*) FROM search_events s WHERE s.visitor_id = o.visitor_id AND s.method IN ('text', 'voice', 'image')
           AND s.occurred_at <= o.first_open))
         FILTER (WHERE first_open IS NOT NULL) AS median_messages_to_open
FROM outcome o"""


def pct(x) -> str:
    return "-" if x is None else f"{100 * float(x):.1f}%"


def report(conn, days: int) -> list[str]:
    p = {"days": days}
    alerts: list[str] = []
    print(f"Last {days} day(s)\n")

    print("Model calls")
    latency = {r["job"]: r for r in conn.execute(REQUEST_LATENCY, p)}
    for r in conn.execute(CALLS, p):
        lat = latency.get(r["job"], {})
        print(f"  {r['job']}: {r['calls']} calls, failed {pct(r['failure_rate'])} "
              f"({r['format_failures']} invalid output), fallback {pct(r['fallback_rate'])}, "
              f"{lat.get('requests', 0)} requests, p50 {lat.get('p50_ms') or 0:.0f} ms, "
              f"p95 {lat.get('p95_ms') or 0:.0f} ms, tokens in/out {r['input_tokens'] or 0}/{r['output_tokens'] or 0}")
        if r["failure_rate"] > MAX_FAILURE_RATE:
            alerts.append(f"{r['job']}: {pct(r['failure_rate'])} of model calls failed")
        if r["fallback_rate"] > MAX_FALLBACK_RATE:
            alerts.append(f"{r['job']}: fallback model answered {pct(r['fallback_rate'])} of calls")
        if (lat.get("p95_ms") or 0) > MAX_P95_MS.get(r["job"], float("inf")):
            alerts.append(f"{r['job']}: slowest 5% of requests take over {lat['p95_ms'] / 1000:.0f} s")

    for r in conn.execute(NEW_SERVED_BY, p):
        alerts.append(f"{r['model']} is now reported as {r['served_by']} (since {r['first_seen']:%Y-%m-%d %H:%M}): "
                      "rerun the level-1 evaluation")
    for r in conn.execute(PROMPT_EDITS):
        alerts.append(f"prompt version {r['prompt_version']} ran with {len(r['hashes'])} different prompts: "
                      "bump the version when a prompt changes")

    d = conn.execute(DRAFT_CHECK, p).fetchone()
    print(f"\nDraft check: {d['drafts']} drafts, flagged {pct(d['flagged_rate'])}, "
          f"rewritten {pct(d['rewritten_rate'])}, unchecked {pct(d['unchecked_rate'])}")
    if d["drafts"] and d["unchecked_rate"] > MAX_UNCHECKED_RATE:
        alerts.append(f"{pct(d['unchecked_rate'])} of drafts reached agents unchecked")

    s = conn.execute(SEARCHES, p).fetchone()
    t = conn.execute(SEARCH_TASKS, p).fetchone()
    print(f"\nTyped searches: {s['typed']}, failed {pct(s['error_rate'])}, declined {pct(s['declined_rate'])}, "
          f"no results {pct(s['zero_result_rate'])}")
    median = t["median_messages_to_open"]
    print(f"Search visits: {t['visits']}, opened a listing {pct(t['opened_rate'])}, "
          f"revealed a phone {pct(t['revealed_rate'])}, median typed messages to first open "
          + ("-" if median is None else f"{median:.0f}"))
    if s["typed"] and s["error_rate"] > MAX_SEARCH_ERROR_RATE:
        alerts.append(f"{pct(s['error_rate'])} of typed searches failed")

    print("\nAlerts:" if alerts else "\nNo alerts.")
    for a in alerts:
        print(f"  ! {a}")
    return alerts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    with psycopg.connect(config.DATABASE_URL, row_factory=dict_row) as conn:
        alerts = report(conn, args.days)
    sys.exit(1 if alerts else 0)


if __name__ == "__main__":
    main()
