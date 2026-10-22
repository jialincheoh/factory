-- Agents pick listings for the homepage's "Featured listings" section; newest pick first.
ALTER TABLE listings ADD COLUMN featured_at TIMESTAMPTZ;
