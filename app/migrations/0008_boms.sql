-- Shelfmark 0008: projects, BOM revisions and lines (M3). See docs/M3.md.

CREATE TABLE project (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE bom_revision (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  label TEXT NOT NULL,                 -- what the operator calls this revision
  source_filename TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  stored_path TEXT,                    -- relative to the asset root, like part assets
  line_count INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  imported_by TEXT NOT NULL,
  note TEXT,
  UNIQUE (project_id, source_sha256)   -- the same file twice is not a revision
);

CREATE TABLE bom_line (
  id INTEGER PRIMARY KEY,
  revision_id INTEGER NOT NULL REFERENCES bom_revision(id) ON DELETE CASCADE,
  part_id INTEGER REFERENCES part(id) ON DELETE SET NULL,   -- NULL = unmatched
  mpn TEXT NOT NULL,
  mpn_norm TEXT NOT NULL,
  manufacturer TEXT,
  manufacturer_norm TEXT,
  description TEXT,
  value TEXT,
  footprint TEXT,                      -- as KiCad wrote it
  package TEXT,                        -- normalised, for grouping and display
  quantity INTEGER NOT NULL,
  references_json TEXT NOT NULL DEFAULT '[]',
  dnp INTEGER NOT NULL DEFAULT 0,
  match_status TEXT NOT NULL CHECK (match_status IN ('matched','unmatched','ambiguous')),
  match_note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX bom_line_revision_idx ON bom_line(revision_id);
CREATE INDEX bom_line_part_idx ON bom_line(part_id);
