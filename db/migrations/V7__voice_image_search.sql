-- Buyers can also search by voice message or photo. The clip or photo itself is
-- never stored; query_text holds the transcript (voice) or the caption typed with
-- the photo, contact details masked as for typed searches.
ALTER TABLE search_events DROP CONSTRAINT search_events_method_check;
ALTER TABLE search_events ADD CONSTRAINT search_events_method_check
  CHECK (method IN ('text', 'filters', 'browse', 'voice', 'image'));
