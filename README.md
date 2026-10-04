# Listing Ad Pipeline

An AI system that writes several ad versions for each property listing, records which version the human real estate agent keeps, and tracks how the published ad performs.

The goal is to learn from two signals at once: what real estate agents actually keep, and what actually brings in inquiries and sales. Prior research suggests people do not always choose the option they rate as best, so the system logs every draft, including the rejected ones.

**Status:** database schema only. The services in the design below are not built yet.

**Terminology:** in this project, *agent* always means a **human real estate agent**. The AI side is a language model called by a generation worker; nothing in this system is an autonomous AI agent.

## Design

The system runs on two clocks that share one Postgres database:

- **Online path (seconds).** Real estate agent's app → API service → job queue → generation worker → language model, with an optional reranker. The worker stores every AI draft with its display position and shuffle seed. The real estate agent keeps one draft, edits it, writes their own, or rejects all of them.
- **Feedback path (days to weeks).** Importers pull daily stats from listing portals and inquiries from the CRM. A nightly export trains the reranker, which is then redeployed.

The reranker starts as random order on purpose, so the first months of data show position effects before any model shapes what real estate agents see.

## Repository layout

```
db/migrations/V1__initial_schema.sql   schema (first migration)
tests/test_schema.sql                  constraint tests, rolled back after running
docker-compose.yml                     local Postgres with the schema applied
.env.example                           local settings template
```

## Run locally

Requires Docker.

```sh
cp .env.example .env                  # then set a password
docker compose up -d                  # starts Postgres and applies migrations
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f /tests/test_schema.sql'
```

The tests print one `pass:` line per check and end with `All schema tests passed.` They run inside a transaction and leave no data behind.

Migrations run only when the database volume is first created. To rebuild from scratch: `docker compose down -v && docker compose up -d`.

## Schema decisions

1. **Rejected drafts are stored, not discarded.** Without them there is no way to tell whether real estate agents kept the draft they would rank highest.
2. **Display position and shuffle seed are logged.** This separates "real estate agents keep the best draft" from "real estate agents keep whatever appears first."
3. **Every decision is explicit:** `kept`, `kept_none` or `wrote_own`. A missing decision row means the request was abandoned. The database guarantees that a kept draft and any rankings belong to the same request.
4. **Re-imports cannot double-count.** Events are keyed by the source system's own ID, and daily portal totals are upserted.
5. **The full prompt and raw model response are stored.** LLM output is not reproducible, so a version label alone cannot show what the real estate agent saw.
6. **No buyer personal data in these tables.** Inquiries are counted here; contact details stay in the CRM.
7. **Listings end explicitly.** `outcomes.status` is `sold`, `withdrawn` or `expired`; a price is required only for sales. No outcome row means the listing is still active.

## Open questions

- **Attribution.** Buyers often phone the real estate agent directly, so some inquiries cannot be traced to a specific ad.
- **Late outcomes.** A sale lands weeks after the ad. How should late events join back to the request, and should training wait for them?
- **Model hosting.** Drafts for one listing share a long prompt. At this scale, is self-hosted serving (for example vLLM with prefix caching) worth it compared with an API?

## Data rules

- Never commit real client, buyer or listing data. Tests use made-up records only.
- Secrets live in `.env`, which is git-ignored.
- Never edit an applied migration. Add `V2__...`, `V3__...` instead.
