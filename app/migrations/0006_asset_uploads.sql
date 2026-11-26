-- Shelfmark 0006: human-supplied assets (M1.8).
-- See docs/M1.8.md.

ALTER TABLE part_asset ADD COLUMN source TEXT NOT NULL DEFAULT 'digikey';
ALTER TABLE part_asset ADD COLUMN uploaded_by TEXT;
ALTER TABLE part_asset ADD COLUMN original_filename TEXT;
