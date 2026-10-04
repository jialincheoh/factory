-- V1__initial_schema.sql
-- First migration for the listing ad pipeline. Apply with a migration tool
-- (Flyway naming shown); never edit this file after it has been applied.
-- Later changes go in V2__..., V3__..., and so on.

-- One row per agent, so agent_id cannot point at a phantom agent
CREATE TABLE agents (
  agent_id      UUID PRIMARY KEY,
  display_name  TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per listing
CREATE TABLE listings (
  listing_id     UUID PRIMARY KEY,
  agent_id       UUID NOT NULL REFERENCES agents,
  property_type  TEXT,            -- condo, landed, etc.
  price_listed   NUMERIC,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row each time the agent asks for ad options
CREATE TABLE generation_requests (
  request_id      UUID PRIMARY KEY,
  listing_id      UUID NOT NULL REFERENCES listings,
  agent_id        UUID NOT NULL REFERENCES agents,
  model_name      TEXT NOT NULL,   -- e.g. gemini-x
  prompt_version  TEXT NOT NULL,   -- so you can compare prompt changes
  prompt_text     TEXT NOT NULL,   -- full prompt sent; LLM output is not reproducible
  experiment_arm  TEXT,            -- which A/B condition
  order_seed      BIGINT NOT NULL, -- seed used to shuffle display order
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every option generated, including the rejected ones
CREATE TABLE candidates (
  candidate_id     UUID PRIMARY KEY,
  request_id       UUID NOT NULL REFERENCES generation_requests,
  display_position SMALLINT NOT NULL CHECK (display_position >= 1),  -- 1 = shown first
  ad_text          TEXT NOT NULL,
  raw_response     JSONB,              -- full model output, for audit
  model_score      REAL,               -- reranker score, if any
  latency_ms       INT,
  cost_usd         NUMERIC(10,6),
  UNIQUE (request_id, candidate_id),   -- target for composite FKs below
  UNIQUE (request_id, display_position)
);

-- What the agent decided (one row per request; no row = abandoned)
CREATE TABLE decisions (
  request_id         UUID PRIMARY KEY REFERENCES generation_requests,
  decision_type      TEXT NOT NULL
                     CHECK (decision_type IN ('kept', 'kept_none', 'wrote_own')),
  kept_candidate_id  UUID,
  final_text         TEXT,             -- after the agent's edits
  edit_distance      INT,              -- how much they changed it
  decided_at         TIMESTAMPTZ NOT NULL,
  seconds_to_decide  INT,
  CHECK ((decision_type = 'kept') = (kept_candidate_id IS NOT NULL)),
  -- kept option must come from this same request
  FOREIGN KEY (request_id, kept_candidate_id)
    REFERENCES candidates (request_id, candidate_id)
);

-- Optional: agent's creativity ranking (Study 1 in the field)
CREATE TABLE rankings (
  request_id    UUID NOT NULL,
  candidate_id  UUID NOT NULL,
  rank          SMALLINT NOT NULL CHECK (rank >= 1),  -- 1 = most creative
  PRIMARY KEY (request_id, candidate_id),
  UNIQUE (request_id, rank),           -- each rank used once
  FOREIGN KEY (request_id, candidate_id)
    REFERENCES candidates (request_id, candidate_id)
);

-- The ad that actually went live
CREATE TABLE published_ads (
  ad_id         UUID PRIMARY KEY,
  request_id    UUID REFERENCES generation_requests,  -- NULL = not from the system
  listing_id    UUID NOT NULL REFERENCES listings,
  channel       TEXT NOT NULL,         -- PropertyGuru, Facebook, own site
  published_at  TIMESTAMPTZ NOT NULL
);

-- Append-only individual events (clicks, inquiries, viewings), never updated.
-- No buyer names or contact details here; those stay in the CRM.
CREATE TABLE ad_events (
  event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ad_id            UUID NOT NULL REFERENCES published_ads,
  event_type       TEXT NOT NULL
                   CHECK (event_type IN ('click', 'inquiry', 'viewing')),
  occurred_at      TIMESTAMPTZ NOT NULL,
  source           TEXT NOT NULL,      -- portal API, manual entry
  source_event_id  TEXT NOT NULL,      -- ID from the source system
  UNIQUE (source, source_event_id)     -- re-imports cannot double-count
);

-- Daily totals, because portals usually report aggregates, not single impressions
CREATE TABLE ad_daily_stats (
  ad_id         UUID NOT NULL REFERENCES published_ads,
  stat_date     DATE NOT NULL,
  impressions   INT NOT NULL DEFAULT 0,
  clicks        INT NOT NULL DEFAULT 0,
  inquiries     INT NOT NULL DEFAULT 0,
  source        TEXT NOT NULL,
  imported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (ad_id, stat_date, source)  -- re-import upserts, never duplicates
);

-- Final outcome per listing; no row = still active
CREATE TABLE outcomes (
  listing_id     UUID PRIMARY KEY REFERENCES listings,
  status         TEXT NOT NULL
                 CHECK (status IN ('sold', 'withdrawn', 'expired')),
  ended_at       TIMESTAMPTZ NOT NULL,   -- date sold, withdrawn or expired
  sold_price     NUMERIC,
  days_on_market INT,
  CHECK ((status = 'sold') = (sold_price IS NOT NULL))
);

-- Postgres does not index foreign-key columns automatically.
-- candidates and rankings are already covered by their unique constraints.
CREATE INDEX listings_agent_idx            ON listings (agent_id);
CREATE INDEX generation_requests_listing_idx ON generation_requests (listing_id);
CREATE INDEX generation_requests_agent_idx ON generation_requests (agent_id);
CREATE INDEX published_ads_listing_idx     ON published_ads (listing_id);
CREATE INDEX published_ads_request_idx     ON published_ads (request_id);
CREATE INDEX ad_events_ad_time_idx         ON ad_events (ad_id, occurred_at);
