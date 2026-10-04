# Listing Ad Pipeline

An AI system that writes several ad versions for each property listing, records which version the human real estate agent keeps, and tracks how the published ad performs.

The goal is to learn from two signals at once: what real estate agents actually keep, and what actually brings in inquiries and sales. Prior research suggests people do not always choose the option they rate as best, so the system logs every draft, including the rejected ones.

**Status:** database schema only. The services in the design below are not built yet.

**Terminology:** in this project, *agent* always means a **human real estate agent**. The AI side is a language model called by a generation worker; nothing in this system is an autonomous AI agent.

## Design

![Overview: the AI writes 5 ad drafts, the human real estate agent keeps one, the ad goes live, results come back, and the system learns which drafts work](docs/overview.svg)

The system runs on two clocks that share one Postgres database. In the detailed diagram below, **blue** is the online path (seconds) and **amber** is the feedback path (days to weeks).

![System design: online path, feedback path and the shared Postgres system of record](docs/architecture.svg)

- **Online path (seconds).** Real estate agent's app → API service → job queue → generation worker → language model, with an optional reranker. The worker stores every AI draft with its display position and shuffle seed. The real estate agent keeps one draft, edits it, writes their own, or rejects all of them.
- **Feedback path (days to weeks).** Ads are published on our own property portal, which logs views, clicks, inquiries and phone-number reveals first-hand. A nightly export trains the reranker, which is then redeployed.

The reranker starts as random order on purpose, so the first months of data show position effects before any model shapes what real estate agents see.

## How it works, in plain language

The system is two loops that share one filing cabinet.

### The fast loop (blue): seconds, while the real estate agent waits

1. **Real estate agent:** a person opens the app and asks for ad ideas for a listing.
2. **API service, the front desk:** receives the request. It also quietly decides which version of the system this person gets, the way a shop tests two window displays, and how the drafts will be shuffled.
3. **Job queue, the waiting line:** writing ads takes a few seconds, so requests wait in line instead of freezing the app. If something fails, the job is retried.
4. **Generation worker, the writer's assistant:** takes the next job and prepares the instructions for the AI.
5. **LLM serving, the AI itself:** writes five different ad drafts.
6. **Reranker, a judge (switched off at first):** later, it will score the drafts to predict which one the real estate agent will like. For now, drafts are shown in random order on purpose, to see whether real estate agents simply pick whatever appears first.
7. **The real estate agent** sees the five drafts, then keeps one, edits it, writes their own, or rejects them all.
8. **Publish:** the kept ad goes live on our own property portal.

### The filing cabinet (middle): Postgres

Everything is written down here: every draft, including the rejected ones, what the real estate agent chose, and how ads perform later. It is the single source of truth both loops share. Buyers' names and contact details are never stored here.

### The slow loop (amber): days and weeks, learning from results

1. **Our property portal:** records every buyer action on each ad as it happens: views, clicks, inquiries, and clicks to reveal the real estate agent's phone number. The number stays hidden until a buyer clicks, so phone interest is linked to the exact ad too.
2. **Event logger, the record keeper:** files each action once. If the same action is reported twice, it is only counted once.
3. **Analytics export:** every night, a copy of the data goes somewhere it can be studied without slowing down the app.
4. **Train reranker, the learning step:** looks at which drafts real estate agents kept and which ads brought in inquiries and sales, and learns what a good draft looks like.
5. **Deploy new version:** the improved judge is switched on in the fast loop, so future drafts get better.

## Repository layout

```
db/migrations/V1__initial_schema.sql   schema (first migration)
tests/test_schema.sql                  constraint tests, rolled back after running
docker-compose.yml                     local Postgres with the schema applied
.env.example                           local settings template
docs/overview.svg                      simple five-step loop
docs/architecture.svg                  detailed system design
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
4. **Events cannot double-count.** Events are keyed by their source and event ID, and daily rollups are upserted when a day is recomputed.
5. **The full prompt and raw model response are stored.** LLM output is not reproducible, so a version label alone cannot show what the real estate agent saw.
6. **No buyer personal data in these tables.** Inquiries are counted here; contact details stay in the CRM.
7. **Listings end explicitly.** `outcomes.status` is `sold`, `withdrawn` or `expired`; a price is required only for sales. No outcome row means the listing is still active.

## Open questions

- **Phone reveals as a signal.** The real estate agent's phone number is hidden until a buyer clicks to reveal it, and each reveal is logged against the ad. A reveal shows interest but does not confirm that a call happened. Is that good enough, or should calls be confirmed later?
- **Showing buyers different drafts.** Because we own the portal, different buyers could be shown different drafts of the same listing at random. That would test directly whether the draft the real estate agent kept is the one buyers respond to most. It needs the real estate agents' agreement and ethics approval first.
- **Late outcomes.** A sale lands weeks after the ad. How should late events join back to the request, and should training wait for them?
- **Model hosting.** Drafts for one listing share a long prompt. At this scale, is self-hosted serving (for example vLLM with prefix caching) worth it compared with an API?

## Data rules

- Never commit real client, buyer or listing data. Tests use made-up records only.
- The portal holds buyer data, so privacy law applies to it directly. Buyer contact details stay in the CRM, never in the research tables.
- Secrets live in `.env`, which is git-ignored.
- Never edit an applied migration. Add `V2__...`, `V3__...` instead.
