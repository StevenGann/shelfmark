"""SQLite access and the migration runner.

One process owns the database (the container runs with a single worker), so we
open a connection per request rather than pooling.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .config import settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # FastAPI resolves the connection dependency in the event-loop thread but
    # runs the (sync) endpoint in a worker thread, so a per-request connection
    # crosses a thread boundary. One request owns one connection and never
    # shares it, so disabling the same-thread check is safe here.
    conn = sqlite3.connect(settings.db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def connection() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, closed afterwards."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    return {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}


def migrate() -> list[str]:
    """Apply pending .sql files in filename order. Returns the names applied."""
    applied: list[str] = []
    conn = connect()
    try:
        done = _applied(conn)
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migration (name) VALUES (?)", (path.name,))
            conn.commit()
            applied.append(path.name)
    finally:
        conn.close()
    return applied


def check() -> bool:
    """True when the database answers and has been migrated."""
    try:
        conn = connect()
        try:
            conn.execute("SELECT 1").fetchone()
            row = conn.execute(
                "SELECT count(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name='schema_migration'"
            ).fetchone()
            return bool(row and row["n"])
        finally:
            conn.close()
    except sqlite3.Error:
        return False
