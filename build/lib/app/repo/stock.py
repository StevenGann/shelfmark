"""Repository functions for stock levels and the stock ledger.

Stock is a ``stock_item`` row per (part, location) and an append-only
``stock_move`` ledger. Every quantity change owns one transaction — begin,
read the row, check, insert the move, update the quantity, commit — the same
shape as IPN allocation in ``parts.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

KINDS = ("receive", "consume", "adjust", "scrap", "build")
SUBTRACT_KINDS = ("consume", "scrap", "build")


class StockError(Exception):
    """A stock move cannot be applied (e.g. it would take a row below zero)."""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collapse_location(location: str) -> str:
    """Trim and collapse internal whitespace, preserving case."""
    return " ".join(location.split())


def location_norm(location: str) -> str:
    """The canonical key for a location: lower(trim(collapse_spaces))."""
    return collapse_location(location).lower()


def _row_to_item(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "part_id": row["part_id"],
        "location": row["location"],
        "location_norm": row["location_norm"],
        "quantity": row["quantity"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _item(conn: sqlite3.Connection, item_id: int) -> dict:
    row = conn.execute("SELECT * FROM stock_item WHERE id = ?", (item_id,)).fetchone()
    assert row is not None
    return _row_to_item(row)


def get_stock(conn: sqlite3.Connection, part_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM stock_item WHERE part_id = ? ORDER BY location_norm, id",
        (part_id,),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def record_move(
    conn: sqlite3.Connection,
    part_id: int,
    location: str,
    kind: str,
    quantity: int,
    note: str | None,
    actor: str,
) -> dict:
    """Apply one stock movement and return the updated stock item.

    ``quantity`` is a positive count to add for ``receive``, a positive count to
    remove for ``consume``/``scrap``/``build`` (the sign comes from ``kind``),
    and the counted total for ``adjust``.
    """
    if kind not in KINDS:
        raise ValueError(f"invalid kind: {kind}")
    quantity = int(quantity)
    if kind == "adjust":
        if quantity < 0:
            raise ValueError("quantity must not be negative")
    elif quantity < 1:
        raise ValueError("quantity must be positive")

    location = collapse_location(location)
    if not location:
        raise ValueError("location is required")
    norm = location_norm(location)
    now = _utc_now()

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM stock_item WHERE part_id = ? AND location_norm = ?",
            (part_id, norm),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO stock_item "
                "(part_id, location, location_norm, quantity, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (part_id, location, norm, now, now),
            )
            assert cur.lastrowid is not None
            item_id = int(cur.lastrowid)
            current = 0
        else:
            item_id = int(row["id"])
            current = int(row["quantity"])

        if kind == "receive":
            delta = quantity
        elif kind in SUBTRACT_KINDS:
            delta = -quantity
        else:  # adjust: quantity is the counted total, the delta is recorded
            delta = quantity - current

        resulting = current + delta
        if resulting < 0:
            raise StockError(
                f"only {current} in stock at {location}; "
                f"{kind} of {quantity} would leave {resulting}"
            )

        conn.execute(
            "INSERT INTO stock_move "
            "(stock_item_id, kind, delta, resulting_quantity, note, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, delta, resulting, note, actor, now),
        )
        conn.execute(
            "UPDATE stock_item SET quantity = ?, updated_at = ? WHERE id = ?",
            (resulting, now, item_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return _item(conn, item_id)


def move_between(
    conn: sqlite3.Connection,
    part_id: int,
    from_location: str,
    to_location: str,
    quantity: int,
    actor: str,
) -> dict:
    """Move *quantity* between two locations of the same part.

    The source is decremented and the destination merged into (or created).
    The ledger records two ``move`` rows, ``-quantity`` on the source and
    ``+quantity`` on the destination, so a move nets to zero for the part while
    every stock item still satisfies ``quantity == SUM(delta)``.
    """
    quantity = int(quantity)
    if quantity < 1:
        raise ValueError("quantity must be positive")
    from_location = collapse_location(from_location)
    to_location = collapse_location(to_location)
    if not from_location or not to_location:
        raise ValueError("both locations are required")
    from_norm = location_norm(from_location)
    to_norm = location_norm(to_location)
    if from_norm == to_norm:
        raise StockError("source and destination are the same location")

    now = _utc_now()
    note = f"moved {quantity} from {from_location} to {to_location}"

    conn.execute("BEGIN IMMEDIATE")
    try:
        src = conn.execute(
            "SELECT * FROM stock_item WHERE part_id = ? AND location_norm = ?",
            (part_id, from_norm),
        ).fetchone()
        if src is None:
            raise StockError(f"no stock at {from_location}")
        if int(src["quantity"]) < quantity:
            raise StockError(
                f"only {src['quantity']} in stock at {from_location}; cannot move {quantity}"
            )

        dst = conn.execute(
            "SELECT * FROM stock_item WHERE part_id = ? AND location_norm = ?",
            (part_id, to_norm),
        ).fetchone()

        src_id = int(src["id"])
        new_src = int(src["quantity"]) - quantity
        conn.execute(
            "UPDATE stock_item SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_src, now, src_id),
        )
        if dst is None:
            cur = conn.execute(
                "INSERT INTO stock_item "
                "(part_id, location, location_norm, quantity, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (part_id, to_location, to_norm, quantity, now, now),
            )
            assert cur.lastrowid is not None
            dst_id = int(cur.lastrowid)
            new_dst = quantity
        else:
            dst_id = int(dst["id"])
            new_dst = int(dst["quantity"]) + quantity
            conn.execute(
                "UPDATE stock_item SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_dst, now, dst_id),
            )

        conn.execute(
            "INSERT INTO stock_move "
            "(stock_item_id, kind, delta, resulting_quantity, note, actor, created_at) "
            "VALUES (?, 'move', ?, ?, ?, ?, ?)",
            (src_id, -quantity, new_src, note, actor, now),
        )
        conn.execute(
            "INSERT INTO stock_move "
            "(stock_item_id, kind, delta, resulting_quantity, note, actor, created_at) "
            "VALUES (?, 'move', ?, ?, ?, ?, ?)",
            (dst_id, quantity, new_dst, note, actor, now),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"from": _item(conn, src_id), "to": _item(conn, dst_id)}


def total_on_hand(conn: sqlite3.Connection, part_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS n FROM stock_item WHERE part_id = ?",
        (part_id,),
    ).fetchone()
    return int(row["n"])


def list_locations(conn: sqlite3.Connection) -> list[dict]:
    """Distinct locations with the number of parts stocked at each."""
    rows = conn.execute(
        "SELECT MIN(location) AS location, COUNT(*) AS count FROM stock_item "
        "GROUP BY location_norm ORDER BY location_norm"
    ).fetchall()
    return [{"location": r["location"], "count": int(r["count"])} for r in rows]


def list_stock(
    conn: sqlite3.Connection,
    location: str | None = None,
    q: str | None = None,
    below: int | None = None,
) -> list[dict]:
    """Stock rows across parts, optionally filtered by location, search, or a
    ``quantity < below`` low-stock threshold."""
    clauses: list[str] = []
    params: list[object] = []
    if location is not None:
        clauses.append("s.location_norm = ?")
        params.append(location_norm(location))
    if q:
        like = f"%{q}%"
        clauses.append(
            "(p.ipn LIKE ? OR p.mpn LIKE ? OR p.mpn_norm LIKE ? "
            "OR m.name LIKE ? OR p.description LIKE ? OR s.location LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])
    if below is not None:
        clauses.append("s.quantity < ?")
        params.append(below)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = conn.execute(
        "SELECT s.id AS stock_item_id, s.part_id, s.location, s.location_norm, "
        "s.quantity, s.updated_at, p.ipn, p.mpn, p.description, m.name AS manufacturer "
        "FROM stock_item s "
        "JOIN part p ON p.id = s.part_id "
        "JOIN manufacturer m ON m.id = p.manufacturer_id"
        f"{where} ORDER BY s.location_norm, p.ipn",
        params,
    ).fetchall()
    return [dict(r) for r in rows]
