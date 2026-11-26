-- Shelfmark 0009: offers and price breaks (M2). See docs/M2.md.

CREATE TABLE offer (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  distributor_id INTEGER NOT NULL REFERENCES distributor(id),
  sku TEXT NOT NULL,
  packaging TEXT,                      -- 'Cut Tape (CT)', 'Tape & Reel (TR)'
  url TEXT,
  currency TEXT NOT NULL DEFAULT 'USD',
  minimum_quantity INTEGER NOT NULL DEFAULT 1,
  stock_quantity INTEGER,              -- NULL when the supplier does not say
  source TEXT NOT NULL,                -- 'digikey' | 'manual'
  fetched_at TEXT,
  created_by TEXT,
  UNIQUE (part_id, distributor_id, sku)
);

CREATE TABLE price_break (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL REFERENCES offer(id) ON DELETE CASCADE,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_price_micros INTEGER NOT NULL CHECK (unit_price_micros >= 0),
  UNIQUE (offer_id, quantity)
);
