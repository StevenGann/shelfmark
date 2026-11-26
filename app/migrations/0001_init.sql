-- Shelfmark 0001: the machine axis and the schema marker.
-- Machine identity is needed from the first day because feeder locations are
-- per machine, not global; a single-row table is cheaper to migrate than a
-- later NOT NULL column on a populated job table.

CREATE TABLE machine (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    feeder_pattern TEXT NOT NULL DEFAULT '^feeder\s+\d+$',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO machine (name) VALUES ('LumenPnP-1');
