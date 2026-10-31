-- Video tours are off until the listing's agent turns one on.
ALTER TABLE listings ADD COLUMN video_enabled BOOLEAN NOT NULL DEFAULT false;

-- One video tour per listing, made from its own photos (app/video.py). photos_key identifies the
-- photos it was made from, so a change of photos or cover triggers a new one. While a new one
-- renders, file_name and data still hold the previous film.
CREATE TABLE listing_videos (
  listing_id  UUID PRIMARY KEY REFERENCES listings ON DELETE CASCADE,
  photos_key  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('rendering', 'ready', 'failed')),
  attempts    INT NOT NULL DEFAULT 1,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  error       TEXT,
  file_name   TEXT UNIQUE,
  data        BYTEA,
  photo_count INT,
  seconds     NUMERIC(6, 2)
);
