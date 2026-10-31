-- The latest comparison of each listing's asking price with similar listings online (app/market.py).
-- facts_key identifies the listing details it was based on, so a change of price or size triggers a new one.
CREATE TABLE listing_market_checks (
  listing_id     UUID PRIMARY KEY REFERENCES listings ON DELETE CASCADE,
  facts_key      TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ,
  error          TEXT,
  report         JSONB,
  prompt_version TEXT
);

ALTER TABLE model_calls DROP CONSTRAINT model_calls_job_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_job_check
  CHECK (job IN ('write_ads', 'search_chat', 'market_check'));
