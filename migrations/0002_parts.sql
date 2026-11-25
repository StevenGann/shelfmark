-- Shelfmark 0002: parts and internal part numbers (M1).

CREATE TABLE part (
  id INTEGER PRIMARY KEY,
  ipn TEXT NOT NULL UNIQUE,
  mpn TEXT NOT NULL,
  mpn_norm TEXT NOT NULL,
  manufacturer TEXT NOT NULL,
  description TEXT,
  category TEXT,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  commodity_class_id INTEGER,
  design_status TEXT NOT NULL DEFAULT 'active'
    CHECK (design_status IN ('active','nrnd','obsolete','do-not-use')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (mpn_norm, manufacturer)
);
CREATE INDEX part_mpn_idx ON part(mpn_norm);
CREATE INDEX part_category_idx ON part(category);

-- One row. IPNs are never reused, so a deleted part leaves a hole.
CREATE TABLE ipn_counter (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_value INTEGER NOT NULL
);
INSERT INTO ipn_counter (id, last_value) VALUES (1, 0);
