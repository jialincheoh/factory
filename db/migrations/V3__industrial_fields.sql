-- V3__industrial_fields.sql
-- Fields that buyers and tenants of factories, warehouses and land search on.
-- All optional: an agent fills what they know, and search filters skip
-- listings where a field is empty.

ALTER TABLE listings
  ADD COLUMN deal_type          TEXT NOT NULL DEFAULT 'sale'
                                CHECK (deal_type IN ('sale', 'rent')),   -- rent: price is per month
  ADD COLUMN state              TEXT,                 -- e.g. Selangor, Johor, Penang
  ADD COLUMN land_area_acres    NUMERIC(10,3) CHECK (land_area_acres > 0),
  ADD COLUMN ceiling_height_ft  NUMERIC(5,1)  CHECK (ceiling_height_ft > 0),
  ADD COLUMN power_amps         INT           CHECK (power_amps > 0),     -- 3-phase supply as quoted in amps
  ADD COLUMN power_kva          INT           CHECK (power_kva > 0),      -- or as quoted in kVA
  ADD COLUMN tenure             TEXT CHECK (tenure IN ('freehold', 'leasehold')),
  ADD COLUMN lease_years_left   SMALLINT CHECK (lease_years_left > 0),
  ADD COLUMN zoning             TEXT CHECK (zoning IN ('light_industrial', 'medium_industrial', 'heavy_industrial',
                                                       'commercial', 'agricultural', 'residential', 'mixed')),
  ADD COLUMN container_access   BOOLEAN,              -- 40-ft container trailers can reach the loading area
  ADD COLUMN loading_bays       SMALLINT CHECK (loading_bays >= 0),
  ADD COLUMN latitude           DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
  ADD COLUMN longitude          DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180),
  ADD CONSTRAINT lease_years_only_for_leasehold CHECK (lease_years_left IS NULL OR tenure = 'leasehold'),
  ADD CONSTRAINT lat_lng_together CHECK ((latitude IS NULL) = (longitude IS NULL));

-- Factories, warehouses and land, alongside the original residential types.
-- property_type stays free text in the database; the app validates the list.

-- Power in amps, whichever unit the agent quoted. For a 415 V three-phase
-- supply, amps = kVA * 1000 / (415 * sqrt(3)), about 1.39 A per kVA.
ALTER TABLE listings
  ADD COLUMN power_amps_equiv INT GENERATED ALWAYS AS
    (COALESCE(power_amps, round(power_kva * 1000 / (415 * sqrt(3)))::int)) STORED;

CREATE INDEX listings_deal_type_idx ON listings (deal_type, property_type);
