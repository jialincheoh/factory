-- Agent feedback on drafts (human-in-the-loop revision graph). The graph's own
-- checkpoints live in LangGraph's tables (checkpoints, checkpoint_blobs, ...),
-- created by PostgresSaver.setup(); this table is the readable record.
CREATE TABLE draft_revisions (
  revision_id        UUID PRIMARY KEY,
  request_id         UUID NOT NULL,
  candidate_id       UUID NOT NULL,
  feedback           TEXT NOT NULL CHECK (length(feedback) <= 500),   -- contact details masked
  ok                 BOOLEAN NOT NULL,                                -- false if the model call failed
  headline           TEXT,
  ad_text            TEXT,
  unsupported_claims JSONB,                                           -- null if the check did not run
  model_name         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (request_id, candidate_id) REFERENCES candidates (request_id, candidate_id)
);
CREATE INDEX draft_revisions_candidate_idx ON draft_revisions (candidate_id, created_at);

-- Meaning-based search: one embedding per published ad (title, headline, text, features).
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE published_ads ADD COLUMN embedding vector(768);
ALTER TABLE published_ads ADD COLUMN embedding_model TEXT;
