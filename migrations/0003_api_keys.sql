-- Shelfmark 0003: API keys (M1.5). See docs/AUTH.md.

CREATE TABLE api_key (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  key_hash TEXT NOT NULL UNIQUE,
  prefix TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('viewer','engineer','admin')),
  created_at TEXT NOT NULL,
  last_used_at TEXT,
  revoked_at TEXT
);
CREATE INDEX api_key_hash_idx ON api_key(key_hash);
