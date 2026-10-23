-- Property ads must show the agent's registration number
-- (REN, PEA or E number with the Board of Valuers); shown on every listing.
ALTER TABLE real_estate_agents ADD COLUMN registration_no TEXT CHECK (length(registration_no) <= 40);
