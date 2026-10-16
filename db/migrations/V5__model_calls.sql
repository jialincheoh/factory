-- Monitoring: one row per model call (including failed calls and fallback calls).
-- No prompt or reply text is stored here; prompts are versioned in code and the
-- ad prompt actually sent is in generation_requests.prompt_text.

CREATE TABLE model_calls (
  call_id         BIGSERIAL PRIMARY KEY,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  job             TEXT NOT NULL CHECK (job IN ('write_ads', 'search_chat')),
  request_id      UUID REFERENCES generation_requests (request_id),  -- NULL when the request failed
  search_id       BIGINT REFERENCES search_events (search_id),        -- NULL when search logging is off
  step            TEXT,                -- analyse, check_drafts, rewrite_drafts, search_chat
  model           TEXT,                -- configured "provider:model" that was called
  served_by       TEXT,                -- model name the provider reported; a change means a silent model update
  is_fallback     BOOLEAN NOT NULL,
  ok              BOOLEAN NOT NULL,
  error           TEXT,                -- exception class, or InvalidStructuredOutput
  latency_ms      INT CHECK (latency_ms >= 0),
  input_tokens    INT,
  output_tokens   INT,
  prompt_version  TEXT NOT NULL,
  prompt_hash     TEXT NOT NULL,       -- fingerprint of the prompt templates in use
  settings        JSONB NOT NULL       -- sampling and retry settings
);
CREATE INDEX model_calls_time_idx ON model_calls (occurred_at);

-- Searches the assistant turned away because the site does not list that kind of property.
ALTER TABLE search_events ADD COLUMN declined TEXT
  CHECK (declined IN ('residential', 'shop_or_office', 'land_only', 'unrelated'));
