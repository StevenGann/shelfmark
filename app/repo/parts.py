"""Plain repository functions over a sqlite3 connection.

No ORM: every function takes a connection, and the ones that write own their
transaction. The IPN allocation is a single transaction — read the counter,
increment it, insert the part, commit — so two concurrent creates cannot get
the same IPN.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from ..ipn import format_ipn
from ..mpn import normalise_mpn

DESIGN_STATUSES = ("active", "nrnd", "obsolete", "do-not-use")

_ALLOWED_UPDATE_FIELDS = {
    "mpn",
    "manufacturer",
    "description",
    "category",
    "notes",
    "design_status",
}


class DuplicatePartError(Exception):
    """A part with the same normalised MPN and manufacturer already exists."""

    def __init__(self, ipn: str) -> None:
        self.ipn = ipn
        super().__init__(f"duplicate part: {ipn}")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_part(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ipn": row["ipn"],
        "mpn": row["mpn"],
        "mpn_norm": row["mpn_norm"],
        "manufacturer": row["manufacturer"],
        "description": row["description"],
        "category": row["category"],
        "parameters_json": row["parameters_json"],
        "commodity_class_id": row["commodity_class_id"],
        "design_status": row["design_status"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_part(
    conn: sqlite3.Connection,
    *,
    mpn: str,
    manufacturer: str,
    description: str | None = None,
    category: str | None = None,
    notes: str | None = None,
    design_status: str = "active",
) -> dict:
    mpn_norm = normalise_mpn(mpn)
    if design_status not in DESIGN_STATUSES:
        raise ValueError(f"invalid design_status: {design_status}")

    # BEGIN IMMEDIATE takes the write lock up front so the duplicate check and
    # the counter increment are atomic with the insert.
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT ipn FROM part WHERE mpn_norm = ? AND manufacturer = ?",
            (mpn_norm, manufacturer),
        ).fetchone()
        if existing is not None:
            raise DuplicatePartError(existing["ipn"])

        row = conn.execute("SELECT last_value FROM ipn_counter WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("ipn_counter is missing its single row (migration 0002)")
        next_value = row["last_value"] + 1
        ipn = format_ipn(next_value)
        now = _utc_now()
        conn.execute("UPDATE ipn_counter SET last_value = ? WHERE id = 1", (next_value,))
        conn.execute(
            """
            INSERT INTO part (
                ipn, mpn, mpn_norm, manufacturer, description, category,
                design_status, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ipn,
                mpn,
                mpn_norm,
                manufacturer,
                description,
                category,
                design_status,
                notes,
                now,
                now,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    part = get_part(conn, ipn)
    assert part is not None
    return part


def get_part(conn: sqlite3.Connection, ipn: str) -> dict | None:
    row = conn.execute("SELECT * FROM part WHERE ipn = ?", (ipn,)).fetchone()
    return None if row is None else _row_to_part(row)


def list_parts(
    conn: sqlite3.Connection,
    *,
    q: str,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    clauses: list[str] = []
    params: list[object] = []
    if q:
        like = f"%{q}%"
        clauses.append(
            "(ipn LIKE ? OR mpn LIKE ? OR mpn_norm LIKE ?"
            " OR manufacturer LIKE ? OR description LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(f"SELECT count(*) AS n FROM part{where}", params).fetchone()
    rows = conn.execute(
        f"SELECT * FROM part{where} ORDER BY ipn LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_row_to_part(r) for r in rows], int(total["n"])


def update_part(conn: sqlite3.Connection, ipn: str, **fields: object) -> dict | None:
    current = get_part(conn, ipn)
    if current is None:
        return None

    unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    mpn = fields.get("mpn", current["mpn"])
    manufacturer = fields.get("manufacturer", current["manufacturer"])
    if "mpn" in fields:
        if not isinstance(mpn, str):
            raise ValueError("mpn must be a string")
        mpn_norm = normalise_mpn(mpn)
    else:
        mpn_norm = current["mpn_norm"]
    if not isinstance(manufacturer, str):
        raise ValueError("manufacturer must be a string")
    if "design_status" in fields and fields["design_status"] not in DESIGN_STATUSES:
        raise ValueError(f"invalid design_status: {fields['design_status']}")

    existing = conn.execute(
        "SELECT ipn FROM part WHERE mpn_norm = ? AND manufacturer = ? AND ipn != ?",
        (mpn_norm, manufacturer, ipn),
    ).fetchone()
    if existing is not None:
        raise DuplicatePartError(existing["ipn"])

    setters: list[str] = []
    values: list[object] = []
    if "mpn" in fields:
        setters.append("mpn = ?")
        values.append(mpn)
        setters.append("mpn_norm = ?")
        values.append(mpn_norm)
    if "manufacturer" in fields:
        setters.append("manufacturer = ?")
        values.append(manufacturer)
    for name in ("description", "category", "notes", "design_status"):
        if name in fields:
            setters.append(f"{name} = ?")
            values.append(fields[name])
    if not setters:
        return current

    setters.append("updated_at = ?")
    values.append(_utc_now())
    values.append(ipn)
    conn.execute(f"UPDATE part SET {', '.join(setters)} WHERE ipn = ?", values)
    conn.commit()
    return get_part(conn, ipn)


def count_parts(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT count(*) AS n FROM part").fetchone()
    return int(row["n"])
