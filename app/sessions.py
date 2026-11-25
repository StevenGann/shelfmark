"""Server-side sessions for people.

The cookie carries a random 32-byte token; only its SHA-256 is stored, so a
database read does not yield a usable cookie. Sessions expire after 30 days with
a sliding window: ``last_seen_at`` is updated at most once a minute.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import users

SESSION_DAYS = 30
SESSION_MAX_AGE = SESSION_DAYS * 24 * 60 * 60

_last_seen: dict[int, float] = {}
_last_sweep: float = 0.0
SWEEP_INTERVAL = 60


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _utc_now()
    expires = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO user_session (token_hash, user_id, created_at, last_seen_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_hash_token(token), user_id, now, now, expires),
    )
    conn.commit()
    return token


def resolve(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    """Return the session's live user, or None.

    Expired rows are deleted on resolve. A session whose user has been disabled
    resolves to None, so a disabled user is rejected on the next request.
    """
    global _last_sweep
    now = _utc_now()
    # Sweep at most once a minute: an unconditional DELETE would turn every page
    # view into a write transaction.
    if time.monotonic() - _last_sweep > SWEEP_INTERVAL:
        _last_sweep = time.monotonic()
        conn.execute("DELETE FROM user_session WHERE expires_at < ?", (now,))
        conn.commit()
    row = conn.execute(
        "SELECT id, user_id, expires_at FROM user_session WHERE token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None or row["expires_at"] < now:
        return None
    user = users.get_user_by_id(conn, int(row["user_id"]))
    if user is None or user["disabled_at"] is not None:
        return None
    user["session_id"] = int(row["id"])
    return user


def touch(conn: sqlite3.Connection, session_id: int) -> None:
    """Stamp ``last_seen_at`` at most once a minute, so a read stays a read."""
    now = time.monotonic()
    last = _last_seen.get(session_id)
    if last is not None and now - last < 60:
        return
    _last_seen[session_id] = now
    stamp = _utc_now()
    # Sliding expiry: a session in use does not age out at the original 30 days.
    expires = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE user_session SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (stamp, expires, session_id),
    )
    conn.commit()


def end(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM user_session WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


def end_all_for_user(conn: sqlite3.Connection, user_id: int, except_id: int | None = None) -> None:
    if except_id is None:
        conn.execute("DELETE FROM user_session WHERE user_id = ?", (user_id,))
    else:
        conn.execute("DELETE FROM user_session WHERE user_id = ? AND id != ?", (user_id, except_id))
    conn.commit()
