-- Shelfmark 0004: people (M1.6). See docs/AUTH.md.

CREATE TABLE app_user (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('viewer','engineer','admin')),
  created_at TEXT NOT NULL,
  last_login_at TEXT,
  disabled_at TEXT
);
CREATE TABLE user_session (
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX user_session_user_idx ON user_session(user_id);
