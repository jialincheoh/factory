-- Schema constraint tests. Each block either succeeds as expected or
-- confirms that the database rejects bad data with the right error.
-- Everything runs inside one transaction and is rolled back at the end.
--
-- Run: psql -v ON_ERROR_STOP=1 -f tests/test_schema.sql

\set ON_ERROR_STOP 1
BEGIN;

-- Sample data (made up; never use real clients here)
INSERT INTO real_estate_agents VALUES ('a0000000-0000-0000-0000-000000000001', 'Sample Real Estate Agent', now());
INSERT INTO listings VALUES ('10000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'condo', 500000, now());
INSERT INTO generation_requests VALUES
  ('b0000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'sample-model', 'v1', 'sample prompt', NULL, 42, now()),
  ('b0000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'sample-model', 'v1', 'sample prompt', NULL, 7, now());
INSERT INTO candidates (candidate_id, request_id, display_position, ad_text) VALUES
  ('c0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001', 1, 'Draft A'),
  ('c0000000-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000001', 2, 'Draft B'),
  ('c0000000-0000-0000-0000-000000000009', 'b0000000-0000-0000-0000-000000000002', 1, 'Draft from another request');

-- Listing pointing at a real estate agent who does not exist
DO $$ BEGIN
  INSERT INTO listings VALUES ('10000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-0000000000ff', 'condo', 1, now());
  RAISE EXCEPTION 'FAIL: unknown real estate agent was accepted';
EXCEPTION WHEN foreign_key_violation THEN RAISE NOTICE 'pass: unknown real estate agent rejected';
END $$;

-- Two drafts at the same display position
DO $$ BEGIN
  INSERT INTO candidates (candidate_id, request_id, display_position, ad_text)
  VALUES ('c0000000-0000-0000-0000-000000000003', 'b0000000-0000-0000-0000-000000000001', 2, 'Duplicate position');
  RAISE EXCEPTION 'FAIL: duplicate display position was accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'pass: duplicate display position rejected';
END $$;

-- Kept draft taken from a different request
DO $$ BEGIN
  INSERT INTO decisions (request_id, decision_type, kept_candidate_id, decided_at)
  VALUES ('b0000000-0000-0000-0000-000000000001', 'kept', 'c0000000-0000-0000-0000-000000000009', now());
  RAISE EXCEPTION 'FAIL: kept draft from another request was accepted';
EXCEPTION WHEN foreign_key_violation THEN RAISE NOTICE 'pass: kept draft from another request rejected';
END $$;

-- "kept" with no draft, and "kept_none" with a draft
DO $$ BEGIN
  INSERT INTO decisions (request_id, decision_type, kept_candidate_id, decided_at)
  VALUES ('b0000000-0000-0000-0000-000000000001', 'kept', NULL, now());
  RAISE EXCEPTION 'FAIL: kept with no draft was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'pass: kept with no draft rejected';
END $$;

DO $$ BEGIN
  INSERT INTO decisions (request_id, decision_type, kept_candidate_id, decided_at)
  VALUES ('b0000000-0000-0000-0000-000000000001', 'kept_none', 'c0000000-0000-0000-0000-000000000001', now());
  RAISE EXCEPTION 'FAIL: kept_none with a draft was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'pass: kept_none with a draft rejected';
END $$;

-- Valid decisions
INSERT INTO decisions (request_id, decision_type, kept_candidate_id, decided_at)
VALUES ('b0000000-0000-0000-0000-000000000001', 'kept', 'c0000000-0000-0000-0000-000000000001', now());
INSERT INTO decisions (request_id, decision_type, kept_candidate_id, decided_at)
VALUES ('b0000000-0000-0000-0000-000000000002', 'kept_none', NULL, now());

-- Rankings: valid, then a repeated rank
INSERT INTO rankings VALUES
  ('b0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000001', 1),
  ('b0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000002', 2);

DO $$ BEGIN
  INSERT INTO candidates (candidate_id, request_id, display_position, ad_text)
  VALUES ('c0000000-0000-0000-0000-000000000008', 'b0000000-0000-0000-0000-000000000002', 2, 'Second draft');
  INSERT INTO rankings VALUES
    ('b0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000009', 1),
    ('b0000000-0000-0000-0000-000000000002', 'c0000000-0000-0000-0000-000000000008', 1);
  RAISE EXCEPTION 'FAIL: repeated rank was accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'pass: repeated rank rejected';
END $$;

-- Ranking a draft from a different request
DO $$ BEGIN
  INSERT INTO rankings VALUES ('b0000000-0000-0000-0000-000000000001', 'c0000000-0000-0000-0000-000000000009', 3);
  RAISE EXCEPTION 'FAIL: ranking a draft from another request was accepted';
EXCEPTION WHEN foreign_key_violation THEN RAISE NOTICE 'pass: ranking a draft from another request rejected';
END $$;

-- Events: the same source event imported twice
INSERT INTO published_ads VALUES ('d0000000-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'sample-portal', now());
INSERT INTO ad_events (ad_id, event_type, occurred_at, source, source_event_id)
VALUES ('d0000000-0000-0000-0000-000000000001', 'click', now(), 'sample-portal', 'evt-1');

DO $$ BEGIN
  INSERT INTO ad_events (ad_id, event_type, occurred_at, source, source_event_id)
  VALUES ('d0000000-0000-0000-0000-000000000001', 'click', now(), 'sample-portal', 'evt-1');
  RAISE EXCEPTION 'FAIL: duplicate import was accepted';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'pass: duplicate import rejected';
END $$;

-- Daily stats: re-importing a day updates the row instead of adding one
INSERT INTO ad_daily_stats (ad_id, stat_date, impressions, source)
VALUES ('d0000000-0000-0000-0000-000000000001', '2026-10-01', 100, 'sample-portal')
ON CONFLICT (ad_id, stat_date, source) DO UPDATE SET impressions = EXCLUDED.impressions;
INSERT INTO ad_daily_stats (ad_id, stat_date, impressions, source)
VALUES ('d0000000-0000-0000-0000-000000000001', '2026-10-01', 120, 'sample-portal')
ON CONFLICT (ad_id, stat_date, source) DO UPDATE SET impressions = EXCLUDED.impressions;

DO $$ BEGIN
  IF (SELECT count(*) FROM ad_daily_stats) <> 1
     OR (SELECT impressions FROM ad_daily_stats) <> 120 THEN
    RAISE EXCEPTION 'FAIL: daily stats re-import did not upsert';
  END IF;
  RAISE NOTICE 'pass: daily stats re-import upserted';
END $$;

-- Outcomes: price only for sales, and only known statuses
DO $$ BEGIN
  INSERT INTO outcomes VALUES ('10000000-0000-0000-0000-000000000001', 'sold', now(), NULL, 30);
  RAISE EXCEPTION 'FAIL: sale without price was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'pass: sale without price rejected';
END $$;

DO $$ BEGIN
  INSERT INTO outcomes VALUES ('10000000-0000-0000-0000-000000000001', 'withdrawn', now(), 500000, 30);
  RAISE EXCEPTION 'FAIL: withdrawn listing with price was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'pass: withdrawn listing with price rejected';
END $$;

DO $$ BEGIN
  INSERT INTO outcomes VALUES ('10000000-0000-0000-0000-000000000001', 'rented', now(), NULL, 30);
  RAISE EXCEPTION 'FAIL: unknown status was accepted';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'pass: unknown status rejected';
END $$;

INSERT INTO outcomes VALUES ('10000000-0000-0000-0000-000000000001', 'withdrawn', now(), NULL, 30);

\echo 'All schema tests passed.'
ROLLBACK;
