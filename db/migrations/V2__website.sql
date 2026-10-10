-- V2__website.sql
-- Columns and tables the website needs: real estate agent logins, the
-- structured property card, uploaded photos, the AI's analysis of each
-- request, and the text of the ad that went live.

-- Real estate agents sign in to the agent side; buyers see the phone number
-- only after clicking "Show phone number" (logged as a phone_reveal event).
ALTER TABLE real_estate_agents
  ADD COLUMN email         TEXT UNIQUE,
  ADD COLUMN password_hash TEXT,
  ADD COLUMN phone         TEXT;

-- The structured property card. These are columns, not JSON, so the buyer
-- search can filter on them directly.
ALTER TABLE listings
  ADD COLUMN status               TEXT NOT NULL DEFAULT 'draft'
                                  CHECK (status IN ('draft', 'published')),
  ADD COLUMN title                TEXT,
  ADD COLUMN location             TEXT,
  ADD COLUMN bedrooms             SMALLINT CHECK (bedrooms >= 0),
  ADD COLUMN bathrooms            SMALLINT CHECK (bathrooms >= 0),
  ADD COLUMN size_sqft            INT CHECK (size_sqft > 0),
  ADD COLUMN features             TEXT[] NOT NULL DEFAULT '{}',  -- lowercase tags: pool, garden, ...
  ADD COLUMN description_original TEXT,   -- what the real estate agent typed, any language
  ADD COLUMN description_language TEXT,   -- as detected by the model
  ADD COLUMN updated_at           TIMESTAMPTZ NOT NULL DEFAULT now();

-- Photos uploaded by the real estate agent. Files are re-encoded on upload
-- (which strips EXIF, including GPS location) and stored under random names.
CREATE TABLE listing_images (
  image_id     UUID PRIMARY KEY,
  listing_id   UUID NOT NULL REFERENCES listings ON DELETE CASCADE,
  position     SMALLINT NOT NULL CHECK (position >= 1),  -- 1 = cover photo
  file_name    TEXT NOT NULL UNIQUE,
  width        INT NOT NULL,
  height       INT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (listing_id, position)
);

-- What the model returned besides the drafts: the property card it filled,
-- the English rewrite of the description, and any places where the photos
-- and the description disagree.
ALTER TABLE generation_requests
  ADD COLUMN analysis JSONB;

ALTER TABLE candidates
  ADD COLUMN headline TEXT;

-- The text that actually went live (after the real estate agent's edits).
ALTER TABLE published_ads
  ADD COLUMN headline TEXT,
  ADD COLUMN ad_text  TEXT;

CREATE INDEX listings_status_idx ON listings (status);
CREATE INDEX listings_features_idx ON listings USING GIN (features);
