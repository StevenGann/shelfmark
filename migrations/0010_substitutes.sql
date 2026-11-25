-- Shelfmark 0010: substitutions (M4). See docs/M4.md.

CREATE TABLE part_substitute (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  substitute_part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  fit TEXT NOT NULL CHECK (fit IN ('exact','equivalent','form-fit','function')),
  note TEXT,
  created_by TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (part_id, substitute_part_id),
  CHECK (part_id <> substitute_part_id),
  CHECK (fit <> 'form-fit' OR (note IS NOT NULL AND length(trim(note)) > 0))
);

ALTER TABLE part ADD COLUMN value TEXT;
ALTER TABLE part ADD COLUMN value_norm TEXT;
CREATE INDEX part_value_norm ON part(value_norm);
