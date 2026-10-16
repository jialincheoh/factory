-- Ad visuals: each draft proposes the photo to lead with, the model captions every
-- photo, and the agent decides which cover is published. Proposed and published
-- covers are both kept, so cover choice can be compared with page opens.

ALTER TABLE candidates ADD COLUMN cover_position SMALLINT CHECK (cover_position >= 1);  -- photo this draft proposes
ALTER TABLE decisions ADD COLUMN cover_position SMALLINT CHECK (cover_position >= 1);   -- photo the agent published as cover
ALTER TABLE listings ADD COLUMN cover_position SMALLINT NOT NULL DEFAULT 1 CHECK (cover_position >= 1);
ALTER TABLE listing_images ADD COLUMN caption TEXT CHECK (length(caption) <= 200);      -- model's description, used as alt text
