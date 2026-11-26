-- Shelfmark 0011: builds and the OpenPnP handoff (M6). See docs/M6.md.

CREATE TABLE build (
  id            INTEGER PRIMARY KEY,
  revision_id   INTEGER NOT NULL REFERENCES bom_revision(id) ON DELETE CASCADE,
  boards        INTEGER NOT NULL CHECK (boards > 0),
  status        TEXT NOT NULL DEFAULT 'planning',   -- planning | committed | abandoned
  note          TEXT,
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  committed_at  TEXT
);

CREATE TABLE build_line (
  id           INTEGER PRIMARY KEY,
  build_id     INTEGER NOT NULL REFERENCES build(id) ON DELETE CASCADE,
  bom_line_id  INTEGER NOT NULL REFERENCES bom_line(id) ON DELETE CASCADE,
  part_id      INTEGER REFERENCES part(id) ON DELETE SET NULL,
  required     INTEGER NOT NULL,   -- per-board quantity x boards
  consumed     INTEGER NOT NULL DEFAULT 0,
  short        INTEGER NOT NULL DEFAULT 0,   -- as of the last plan
  UNIQUE (build_id, bom_line_id)
);

CREATE TABLE placement (
  id           INTEGER PRIMARY KEY,
  revision_id  INTEGER NOT NULL REFERENCES bom_revision(id) ON DELETE CASCADE,
  reference    TEXT NOT NULL,
  side         TEXT NOT NULL DEFAULT 'Top',
  x_mm         REAL NOT NULL,
  y_mm         REAL NOT NULL,
  rotation_deg REAL NOT NULL DEFAULT 0,
  package      TEXT,
  value        TEXT,
  UNIQUE (revision_id, reference)
);
CREATE INDEX build_revision_idx ON build(revision_id);
CREATE INDEX build_line_build_idx ON build_line(build_id);
CREATE INDEX placement_revision_idx ON placement(revision_id);
