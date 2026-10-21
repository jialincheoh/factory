-- Photos are kept in the database as well as on disk. Hosts with an ephemeral
-- disk (Render's free plan) wipe UPLOAD_DIR on every restart; the disk copy is
-- now only a cache that /uploads refills from here.
CREATE TABLE image_blobs (
  file_name  TEXT PRIMARY KEY,
  data       BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Production goes live with real listings: remove the made-up demo account
-- (scripts/seed_demo.py, whose password is in the repository) and everything it owns.
CREATE TEMP TABLE demo_listings ON COMMIT DROP AS
  SELECT l.listing_id FROM listings l JOIN real_estate_agents a ON a.real_estate_agent_id = l.real_estate_agent_id
  WHERE a.email = 'demo@example.com';
CREATE TEMP TABLE demo_requests ON COMMIT DROP AS
  SELECT request_id FROM generation_requests WHERE listing_id IN (SELECT listing_id FROM demo_listings);

DELETE FROM ad_events WHERE ad_id IN (SELECT ad_id FROM published_ads WHERE listing_id IN (SELECT listing_id FROM demo_listings));
DELETE FROM ad_daily_stats WHERE ad_id IN (SELECT ad_id FROM published_ads WHERE listing_id IN (SELECT listing_id FROM demo_listings));
DELETE FROM published_ads WHERE listing_id IN (SELECT listing_id FROM demo_listings);
DELETE FROM draft_revisions WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM model_calls WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM rankings WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM decisions WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM candidates WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM generation_requests WHERE request_id IN (SELECT request_id FROM demo_requests);
DELETE FROM outcomes WHERE listing_id IN (SELECT listing_id FROM demo_listings);
DELETE FROM image_blobs WHERE file_name IN (SELECT file_name FROM listing_images WHERE listing_id IN (SELECT listing_id FROM demo_listings));
DELETE FROM listings WHERE listing_id IN (SELECT listing_id FROM demo_listings);  -- listing_images cascade
DELETE FROM real_estate_agents WHERE email = 'demo@example.com';
