"""API-key authentication for machines.

Keys are ``smk_`` + 40 hex chars. Only a SHA-256 hash is stored; the plaintext
is shown once at creation and then lives only with its owner. Keys are presented
as ``Authorization: Bearer …`` or ``X-API-Key: …`` and gate ``/api/*`` only.
People use username and password sessions (``app.users``, ``app.sessions``);
this module has no cookie path.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

from .config import settings
from .deps import Conn

logger = logging.getLogger("shelfmark")

ROLE_ORDER = {"viewer": 0, "engineer": 1, "admin": 2}
ROLES = ("viewer", "engineer", "admin")

_last_touched: dict[int, float] = {}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_key() -> tuple[str, str, str]:
    """Return ``(plaintext, sha256_hash, prefix)`` for a fresh key."""
    plaintext = "smk_" + secrets.token_hex(20)
    return plaintext, hash_key(plaintext), plaintext[4:12]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _row_to_key(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "key_hash": row["key_hash"],
        "prefix": row["prefix"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
    }


def _lookup_by_hash(conn: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    digest = hash_key(raw)
    row = conn.execute("SELECT * FROM api_key WHERE key_hash = ?", (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["key_hash"], digest):
        return None
    return _row_to_key(row)


def verify_key(conn: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    """Return the live key row for a presented plaintext key, or None."""
    key = _lookup_by_hash(conn, raw)
    if key is None or key["revoked_at"] is not None:
        return None
    return key


def extract_header_key(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    key = request.headers.get("X-API-Key")
    return key.strip() if key else None


def resolve_key(conn: sqlite3.Connection, request: Request) -> dict[str, Any] | None:
    raw = extract_header_key(request)
    if raw is None:
        return None
    return _lookup_by_hash(conn, raw)


def touch_last_used(conn: sqlite3.Connection, key_id: int) -> None:
    """Stamp ``last_used_at`` at most once a minute per key.

    An every-request write would turn a read-only query into a write transaction;
    remembering the last stamp per key keeps reads cheap.
    """
    now = time.monotonic()
    last = _last_touched.get(key_id)
    if last is not None and now - last < 60:
        return
    _last_touched[key_id] = now
    conn.execute("UPDATE api_key SET last_used_at = ? WHERE id = ?", (_utc_now(), key_id))
    conn.commit()


def require(role: str = "viewer") -> Callable[..., dict[str, Any]]:
    """FastAPI dependency: return the key row, or raise 401/403.

    ``role`` is the minimum role required; ordering is viewer < engineer < admin.
    A revoked key is 401, not 403, so it cannot be distinguished from an
    unknown key by probing.
    """
    if role not in ROLE_ORDER:
        raise ValueError(f"unknown role: {role}")

    def dependency(request: Request, conn: Conn) -> dict[str, Any]:
        if settings.auth == "none":
            return {}
        key = resolve_key(conn, request)
        if key is None or key["revoked_at"] is not None:
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if ROLE_ORDER[key["role"]] < ROLE_ORDER[role]:
            raise HTTPException(status_code=403, detail="insufficient role")
        touch_last_used(conn, int(key["id"]))
        return key

    return dependency


def _insert(
    conn: sqlite3.Connection, name: str, role: str, plaintext: str, key_hash: str, prefix: str
) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO api_key (name, key_hash, prefix, role, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, key_hash, prefix, role, _utc_now()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM api_key WHERE key_hash = ?", (key_hash,)).fetchone()
    return {"id": int(row["id"]), "key": plaintext, "name": name, "role": role}


def create_key(conn: sqlite3.Connection, name: str, role: str) -> dict[str, Any]:
    if role not in ROLE_ORDER:
        raise ValueError(f"invalid role: {role}")
    plaintext, key_hash, prefix = generate_key()
    return _insert(conn, name, role, plaintext, key_hash, prefix)


def list_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, prefix, role, created_at, last_used_at, revoked_at "
        "FROM api_key ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "prefix": r["prefix"],
            "role": r["role"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
            "revoked_at": r["revoked_at"],
        }
        for r in rows
    ]


def revoke_key(conn: sqlite3.Connection, key_id: int) -> bool:
    cur = conn.execute(
        "UPDATE api_key SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (_utc_now(), key_id)
    )
    conn.commit()
    return cur.rowcount > 0


def revoke_key_by_name(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "UPDATE api_key SET revoked_at = COALESCE(revoked_at, ?) WHERE name = ?",
        (_utc_now(), name),
    )
    conn.commit()
    return cur.rowcount > 0


def bootstrap(conn: sqlite3.Connection) -> None:
    """Create the first admin key when the table is empty, exactly once."""
    row = conn.execute("SELECT count(*) AS n FROM api_key").fetchone()
    if int(row["n"]) > 0:
        return
    env_key = settings.bootstrap_key.strip()
    if env_key:
        plaintext = env_key
        where = "SHELFMARK_BOOTSTRAP_KEY"
    else:
        plaintext = generate_key()[0]
        path = settings.data_dir / "bootstrap-key.txt"
        # Create it 0600 rather than writing then chmod'ing: between the two the
        # key would briefly be world-readable under a permissive umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(plaintext + "\n")
        where = str(path)
    _insert(conn, "bootstrap", "admin", plaintext, hash_key(plaintext), plaintext[4:12])
    # The prefix only: stdout is copied to logs far more freely than /data is read.
    logger.warning("BOOTSTRAP KEY created (smk_%s…); read it from %s", plaintext[4:12], where)
