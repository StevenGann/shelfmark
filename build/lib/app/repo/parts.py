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
from ..mpn import normalise_mpn, normalise_value
from .catalog import _ensure_manufacturer

DESIGN_STATUSES = ("active", "nrnd", "obsolete", "do-not-use")

_ALLOWED_UPDATE_FIELDS = {
    "mpn",
    "manufacturer",
    "description",
    "category",
    "notes",
    "design_status",
    "package",
    "value",
}

_PART_SELECT = (
    "SELECT p.*, m.name AS manufacturer, m.website AS manufacturer_website "
    "FROM part p JOIN manufacturer m ON m.id = p.manufacturer_id"
)


def _row_to_part(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ipn": row["ipn"],
        "mpn": row["mpn"],
        "mpn_norm": row["mpn_norm"],
        "manufacturer_id": row["manufacturer_id"],
        "manufacturer": row["manufacturer"],
        "manufacturer_website": row["manufacturer_website"],
        "package": row["package"],
        "description": row["description"],
        "category": row["category"],
        "parameters_json": row["parameters_json"],
        "commodity_class_id": row["commodity_class_id"],
        "design_status": row["design_status"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class DuplicatePartError(Exception):
    """A part with the same normalised MPN and manufacturer already exists."""

    def __init__(self, ipn: str) -> None:
        self.ipn = ipn
        super().__init__(f"duplicate part: {ipn}")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        manufacturer_id = _ensure_manufacturer(conn, manufacturer)
        existing = conn.execute(
            "SELECT ipn FROM part WHERE mpn_norm = ? AND manufacturer_id = ?",
            (mpn_norm, manufacturer_id),
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
                ipn, mpn, mpn_norm, manufacturer_id, description, category,
                design_status, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ipn,
                mpn,
                mpn_norm,
                manufacturer_id,
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
    row = conn.execute(f"{_PART_SELECT} WHERE p.ipn = ?", (ipn,)).fetchone()
    return None if row is None else _row_to_part(row)


def get_part_by_id(conn: sqlite3.Connection, part_id: int) -> dict | None:
    row = conn.execute(f"{_PART_SELECT} WHERE p.id = ?", (part_id,)).fetchone()
    return None if row is None else _row_to_part(row)


def list_parts(
    conn: sqlite3.Connection,
    *,
    q: str,
    category: str | None = None,
    package: str | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str | None = None,
) -> tuple[list[dict], int]:
    clauses: list[str] = []
    params: list[object] = []
    if q:
        like = f"%{q}%"
        clauses.append(
            "(p.ipn LIKE ? OR p.mpn LIKE ? OR p.mpn_norm LIKE ?"
            " OR m.name LIKE ? OR p.description LIKE ? OR p.package LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])
    if category is not None:
        clauses.append("p.category = ?")
        params.append(category)
    if package is not None:
        clauses.append("p.package = ?")
        params.append(package)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(
        f"SELECT count(*) AS n FROM part p JOIN manufacturer m ON m.id = p.manufacturer_id{where}",
        params,
    ).fetchone()
    order = "COALESCE(s.on_hand, 0) ASC, p.ipn ASC" if sort == "onhand" else "p.ipn ASC"
    rows = conn.execute(
        f"SELECT p.*, m.name AS manufacturer, m.website AS manufacturer_website, "
        f"COALESCE(s.on_hand, 0) AS on_hand "
        f"FROM part p JOIN manufacturer m ON m.id = p.manufacturer_id "
        f"LEFT JOIN (SELECT part_id, SUM(quantity) AS on_hand FROM stock_item "
        f"GROUP BY part_id) s ON s.part_id = p.id"
        f"{where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    parts = []
    for row in rows:
        part = _row_to_part(row)
        part["on_hand"] = int(row["on_hand"])
        parts.append(part)
    return parts, int(total["n"])


def update_part(conn: sqlite3.Connection, ipn: str, **fields: object) -> dict | None:
    current = get_part(conn, ipn)
    if current is None:
        return None

    unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")

    mpn = fields.get("mpn", current["mpn"])
    if "mpn" in fields:
        if not isinstance(mpn, str):
            raise ValueError("mpn must be a string")
        mpn_norm = normalise_mpn(mpn)
    else:
        mpn_norm = current["mpn_norm"]
    if "manufacturer" in fields:
        manufacturer = fields["manufacturer"]
        if not isinstance(manufacturer, str):
            raise ValueError("manufacturer must be a string")
        manufacturer_id = _ensure_manufacturer(conn, manufacturer)
    else:
        manufacturer_id = current["manufacturer_id"]
    if "design_status" in fields and fields["design_status"] not in DESIGN_STATUSES:
        raise ValueError(f"invalid design_status: {fields['design_status']}")

    existing = conn.execute(
        "SELECT ipn FROM part WHERE mpn_norm = ? AND manufacturer_id = ? AND ipn != ?",
        (mpn_norm, manufacturer_id, ipn),
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
        setters.append("manufacturer_id = ?")
        values.append(manufacturer_id)
    if "value" in fields:
        raw_value = fields["value"]
        if raw_value is not None and not isinstance(raw_value, str):
            raise ValueError("value must be a string")
        setters.append("value = ?")
        values.append(raw_value)
        setters.append("value_norm = ?")
        values.append(normalise_value(raw_value) if raw_value else None)
    for name in ("package", "description", "category", "notes", "design_status"):
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
