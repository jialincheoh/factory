# Operations

## Production: Render

`render.yaml` describes a Docker web service and a Postgres database, both on the free plan in Singapore. A commit to
`main` deploys automatically. Migrations run as the new version starts, before it serves, and a build that fails to
start leaves the previous version serving.

Free-plan limits to keep in mind:

- The service sleeps when idle, so the first request after a quiet period is slow.
- The disk is wiped on restart. Photos and videos are in Postgres, so this only costs cache refills.
- The free database has an expiry date and must be upgraded or replaced before then.
- There is no shell or cron, so `scripts/boot_jobs.py` starts optional one-off jobs at boot.

## Settings

[`.env.example`](../.env.example) documents the local settings and `app/config.py` is the full list. The ones that
matter in production:

| Setting | Purpose |
|---|---|
| `DATABASE_URL`, `SESSION_SECRET` | Required. |
| `SECURE_COOKIES` | Leave at `1`. Set `0` only on plain http. |
| `SITE_URL`, `SITE_NAME` | Canonical links, sitemap, structured data. |
| `OPERATOR_NAME`, `PRIVACY_EMAIL` | Shown in the privacy notice. Required before launch. |
| `LLM_MODEL`, `LLM_FALLBACK_MODEL`, provider key | Model and fallback, as `provider:model`. |
| `EMBEDDINGS_MODEL` | Turns on meaning-based search. |
| `VIDEO_WORKER` | Video render thread, on by default. Set `0` and use the script where the host limits background threads. |
| `MARKET_CHECK_WORKER` | Market check thread, off by default because it spends search quota. |
| `SEARCH_LOGGING` | Stores searches. `/privacy` describes this and must stay in step with it. |

## Commands

| Command | Purpose |
|---|---|
| `python -m scripts.migrate` | Apply new migrations. Safe to repeat. |
| `python -m scripts.create_agent --email ... --name ... --phone ...` | Create an agent login. |
| `python -m scripts.monitor --days 1` | Health report. Exits with status 1 when an alert fires. |
| `python -m scripts.eval_ads export` / `run` | Build an evaluation set and score a model and prompt. |
| `python -m scripts.render_videos` | Render due video tours. |
| `python -m scripts.market_check` | Run due market checks. |

`scripts.seed_demo` is for local use only. Its listings are made up.

## Changing the code

- **Schema:** add `db/migrations/V<n>__name.sql`. Never edit one that has been applied.
- **Prompts:** change the text, bump the version in `app/ai/llm.py`, and pin the new fingerprint in `tests/test_ai.py`.
  The test fails until both are done.
- **Dependencies:** edit `requirements.in`, then run `make lock`.
- **What buyers' data is stored:** update `app/templates/privacy.html` in the same change, in both languages.

## Google Cloud (not yet exercised)

`deploy/` holds scripts for a larger setup: Cloud Run, Cloud SQL, a storage bucket mounted at `/uploads`, and Secret
Manager. **They have never been run**, so expect to fix them on first use. `deploy/setup.sh` creates billable resources.

```sh
deploy/setup.sh          # once: services, database, bucket, secrets, permissions
deploy/deploy.sh         # each release: build, migrate, deploy
deploy/create-agent.sh --email you@example.com --name "Your Name" --phone "..."
deploy/domain.sh         # once: verify and map the domain
```

## Map tiles

OpenStreetMap's public tile server suits light use. A busier site should switch `MAP_TILES` in
`app/web/templating.py` to a commercial provider, as OpenStreetMap's usage policy asks.
