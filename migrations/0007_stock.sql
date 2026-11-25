-- Shelfmark 0007: stock levels and the stock ledger (M5). See docs/M5.md.

CREATE TABLE stock_item (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  location TEXT NOT NULL,            -- as typed, trimmed, spaces collapsed
  location_norm TEXT NOT NULL,       -- lower(trim(collapsed)), for matching
  quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (part_id, location_norm)
);

CREATE TABLE stock_move (
  id INTEGER PRIMARY KEY,
  stock_item_id INTEGER NOT NULL REFERENCES stock_item(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('receive','consume','adjust','scrap','build','move')),
  delta INTEGER NOT NULL,            -- signed; a move writes -q here and +q there
  resulting_quantity INTEGER NOT NULL,
  note TEXT,
  actor TEXT NOT NULL,               -- username, or a key's name for API callers
  created_at TEXT NOT NULL
);
CREATE INDEX stock_move_item_idx ON stock_move(stock_item_id, created_at);
CREATE INDEX stock_item_location_idx ON stock_item(location_norm);
