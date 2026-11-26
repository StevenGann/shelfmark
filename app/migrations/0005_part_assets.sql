-- Shelfmark 0005: part links, package, and hosted assets (M1.7).
-- See docs/M1.7.md.

CREATE TABLE manufacturer (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL UNIQUE,
  website TEXT
);

-- Backfill manufacturers from the distinct strings already on ``part`` before
-- the part table is rebuilt to reference them. ``name_norm`` is the lower-cased
-- trimmed name; the same rule is used by the enrichment upsert.
INSERT INTO manufacturer (name, name_norm)
SELECT MIN(manufacturer), lower(trim(manufacturer))
FROM part
GROUP BY lower(trim(manufacturer));

-- SQLite cannot add a NOT NULL foreign key to an existing table, so ``part`` is
-- rebuilt: the new shape, copied rows, drop and rename. This must happen before
-- anything else references ``part``.
CREATE TABLE part_new (
  id INTEGER PRIMARY KEY,
  ipn TEXT NOT NULL UNIQUE,
  mpn TEXT NOT NULL,
  mpn_norm TEXT NOT NULL,
  manufacturer_id INTEGER NOT NULL REFERENCES manufacturer(id),
  package TEXT,
  description TEXT,
  category TEXT,
  parameters_json TEXT NOT NULL DEFAULT '{}',
  commodity_class_id INTEGER,
  design_status TEXT NOT NULL DEFAULT 'active'
    CHECK (design_status IN ('active','nrnd','obsolete','do-not-use')),
  notes TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (mpn_norm, manufacturer_id)
);

INSERT INTO part_new (
  id, ipn, mpn, mpn_norm, manufacturer_id, description, category,
  parameters_json, commodity_class_id, design_status, notes, created_at, updated_at
)
SELECT p.id, p.ipn, p.mpn, p.mpn_norm, m.id, p.description, p.category,
       p.parameters_json, p.commodity_class_id, p.design_status, p.notes,
       p.created_at, p.updated_at
FROM part p
JOIN manufacturer m ON m.name_norm = lower(trim(p.manufacturer));

DROP TABLE part;
ALTER TABLE part_new RENAME TO part;

CREATE INDEX part_mpn_idx ON part(mpn_norm);
CREATE INDEX part_category_idx ON part(category);
CREATE INDEX part_manufacturer_idx ON part(manufacturer_id);

CREATE TABLE distributor (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_norm TEXT NOT NULL UNIQUE,
  search_url_template TEXT NOT NULL,   -- contains {mpn}
  product_url_template TEXT             -- contains {sku}
);

CREATE TABLE part_link (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('datasheet','manufacturer','distributor')),
  label TEXT NOT NULL,
  url TEXT NOT NULL,
  distributor_id INTEGER REFERENCES distributor(id),
  sku TEXT,
  source TEXT NOT NULL,                -- 'digikey', 'manual', 'template'
  fetched_at TEXT,
  UNIQUE (part_id, kind, url)
);

CREATE TABLE part_asset (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('image','datasheet')),
  source_url TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  content_type TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','failed')),
  error TEXT,
  UNIQUE (part_id, kind, source_url)
);

CREATE TABLE fetch_job (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                  -- 'enrich'
  status TEXT NOT NULL,                -- queued|running|done|failed
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE INDEX fetch_job_status_idx ON fetch_job(status, next_attempt_at);

-- Distributor seeds. Search templates carry the MPN; Mouser also gets a product
-- template that takes a SKU once a Mouser API key arrives.
INSERT INTO distributor (name, name_norm, search_url_template, product_url_template) VALUES
  ('DigiKey', 'digikey', 'https://www.digikey.com/en/products/result?keywords={mpn}', NULL),
  ('Mouser', 'mouser', 'https://www.mouser.com/c/?q={mpn}', 'https://www.mouser.com/ProductDetail/{sku}'),
  ('LCSC', 'lcsc', 'https://www.lcsc.com/search?q={mpn}', NULL),
  ('TME', 'tme', 'https://www.tme.eu/en/katalog/?search={mpn}', NULL),
  ('Farnell', 'farnell', 'https://uk.farnell.com/search?st={mpn}', NULL),
  ('Octopart', 'octopart', 'https://octopart.com/search?q={mpn}', NULL);
