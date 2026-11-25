"""People: usernames and password hashing.

Passwords are hashed with scrypt from the standard library and stored in the
self-describing ``scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`` format from
``docs/AUTH.md``. The parameters live in the stored string so they can be raised
later and rehashed on the next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .auth import ROLE_ORDER

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "username_norm": row["username_norm"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "disabled_at": row["disabled_at"],
    }


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${digest_b64}"


def _parse_stored(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    try:
        scheme, n_s, r_s, p_s, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return None
    if scheme != "scrypt":
        return None
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return None
    return n, r, p, salt, expected


def verify_password(stored: str, password: str) -> bool:
    parsed = _parse_stored(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        digest = hashlib.scrypt(
            password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=SCRYPT_MAXMEM
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected)


def needs_rehash(stored: str) -> bool:
    parsed = _parse_stored(stored)
    if parsed is None:
        return True
    n, r, p, _, _ = parsed
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def validate_password(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if password == username:
        raise ValueError("Password must not be the same as the username.")


def create_user(
    conn: sqlite3.Connection, username: str, password: str, role: str
) -> dict[str, Any]:
    if role not in ROLE_ORDER:
        raise ValueError(f"invalid role: {role}")
    username = username.strip()
    if not username:
        raise ValueError("Username is required.")
    validate_password(password, username)
    conn.execute(
        "INSERT INTO app_user (username, username_norm, password_hash, role, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, username.lower(), hash_password(password), role, _utc_now()),
    )
    conn.commit()
    user = get_user_by_username(conn, username)
    assert user is not None
    return user


def get_user_by_username(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM app_user WHERE username_norm = ?", (username.strip().lower(),)
    ).fetchone()
    return None if row is None else _row_to_user(row)


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else _row_to_user(row)


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM app_user ORDER BY id").fetchall()
    return [_row_to_user(r) for r in rows]


def set_role(conn: sqlite3.Connection, user_id: int, role: str) -> None:
    if role not in ROLE_ORDER:
        raise ValueError(f"invalid role: {role}")
    conn.execute("UPDATE app_user SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    conn.execute(
        "UPDATE app_user SET disabled_at = ? WHERE id = ?",
        (_utc_now() if disabled else None, user_id),
    )
    conn.commit()


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute(
        "UPDATE app_user SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
    )
    conn.commit()


def stamp_login(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE app_user SET last_login_at = ? WHERE id = ?", (_utc_now(), user_id))
    conn.commit()


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT count(*) AS n FROM app_user").fetchone()
    return int(row["n"])
