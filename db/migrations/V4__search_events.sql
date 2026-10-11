-- Search logging: how visitors search (a typed sentence, the filter bar, or browsing
-- a landing page), what they typed, the filters it became and how many results came back.
-- visitor_id is a random ID kept in the visitor's session cookie. It links one visit's
-- searches to the listings they then open, and is never linked to an account, name or contact.

CREATE TABLE search_events (
  search_id     BIGSERIAL PRIMARY KEY,
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  visitor_id    UUID NOT NULL,
  method        TEXT NOT NULL CHECK (method IN ('text', 'filters', 'browse')),
  query_text    TEXT CHECK (length(query_text) <= 1000),   -- what was typed (text searches only)
  filters       JSONB NOT NULL,                             -- the filters searched with
  result_count  INT CHECK (result_count >= 0),
  interpreter   TEXT                                        -- model or rule set that read the text
);
CREATE INDEX search_events_visitor_idx ON search_events (visitor_id, occurred_at);
CREATE INDEX search_events_time_idx ON search_events (occurred_at);

ALTER TABLE ad_events ADD COLUMN visitor_id UUID;   -- portal events only; NULL for manual imports
