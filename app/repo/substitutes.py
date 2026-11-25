"""Repository functions for part substitutions.

A substitution is a directed link: ``part_id`` is the part a design calls for,
``substitute_part_id`` is what may be used instead. The reverse is a separate
row, so "a 10 k 1 % will do for a 10 k 5 %" never implies the opposite.

There is no ORM: every function takes a connection and the write helpers own
their transaction, the same shape as the other repositories.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from ..mpn import normalise_mpn
from . import parts as parts_repo
from . import stock as stock_repo

FIT_LEVELS = ("exact", "equivalent", "form-fit", "function")
_FIT_ORDER = {fit: i for i, fit in enumerate(FIT_LEVELS)}


class DuplicateSubstituteError(Exception):
    """The same directed link already exists."""

    def __init__(self, part_id: int, substitute_part_id: int) -> None:
        self.part_id = part_id
        self.substitute_part_id = substitute_part_id
        super().__init__(f"substitute link already exists: {part_id} -> {substitute_part_id}")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_part_ref(conn: sqlite3.Connection, ref: str) -> dict | None:
    """Resolve ``ref`` as an IPN first, then as a normalised MPN (unique only)."""
    part = parts_repo.get_part(conn, ref)
    if part is not None:
        return part
    try:
        mpn_norm = normalise_mpn(ref)
    except ValueError:
        return None
    rows = conn.execute("SELECT id FROM part WHERE mpn_norm = ?", (mpn_norm,)).fetchall()
    if len(rows) != 1:
        return None
    return parts_repo.get_part_by_id(conn, int(rows[0]["id"]))


def _row_to_link(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    part_id = int(row["part_id"])
    substitute_part_id = int(row["substitute_part_id"])
    return {
        "id": int(row["id"]),
        "part_id": part_id,
        "part_ipn": row["part_ipn"],
        "part_mpn": row["part_mpn"],
        "part_package": row["part_package"],
        "part_on_hand": stock_repo.total_on_hand(conn, part_id),
        "substitute_part_id": substitute_part_id,
        "substitute_ipn": row["substitute_ipn"],
        "substitute_mpn": row["substitute_mpn"],
        "substitute_package": row["substitute_package"],
        "substitute_on_hand": stock_repo.total_on_hand(conn, substitute_part_id),
        "fit": row["fit"],
        "note": row["note"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _link_select(direction: str) -> str:
    return (
        "SELECT s.id, s.part_id, s.substitute_part_id, s.fit, s.note, s.created_by, s.created_at, "
        "p.ipn AS part_ipn, p.mpn AS part_mpn, p.package AS part_package, "
        "q.ipn AS substitute_ipn, q.mpn AS substitute_mpn, q.package AS substitute_package "
        "FROM part_substitute s "
        "JOIN part p ON p.id = s.part_id "
        "JOIN part q ON q.id = s.substitute_part_id "
    )


def _fetch_links(conn: sqlite3.Connection, where: str, params: tuple[object, ...]) -> list[dict]:
    rows = conn.execute(_link_select("") + where, params).fetchall()
    links = [_row_to_link(conn, r) for r in rows]
    links.sort(
        key=lambda link: (
            _FIT_ORDER[link["fit"]],
            -int(link["substitute_on_hand"]),
            link["substitute_ipn"],
        )
    )
    return links


def list_out(conn: sqlite3.Connection, part_id: int) -> list[dict]:
    """Links out: the parts that may be used where *part_id* is called for."""
    return _fetch_links(conn, "WHERE s.part_id = ?", (part_id,))


def list_in(conn: sqlite3.Connection, part_id: int) -> list[dict]:
    """Links in: the parts that *part_id* may stand in for."""
    return _fetch_links(conn, "WHERE s.substitute_part_id = ?", (part_id,))


def get_link(conn: sqlite3.Connection, link_id: int) -> dict | None:
    row = conn.execute(_link_select("") + "WHERE s.id = ?", (link_id,)).fetchone()
    return None if row is None else _row_to_link(conn, row)


def add_link(
    conn: sqlite3.Connection,
    part_id: int,
    substitute_part_id: int,
    fit: str,
    note: str | None,
    created_by: str | None,
) -> dict:
    """Add one directed substitute link and return it.

    Self-links are refused, ``form-fit`` requires a non-empty note, and a
    duplicate link raises :class:`DuplicateSubstituteError`.
    """
    if fit not in FIT_LEVELS:
        raise ValueError(f"invalid fit: {fit}")
    if part_id == substitute_part_id:
        raise ValueError("a part cannot substitute for itself")
    if fit == "form-fit" and not (note and note.strip()):
        raise ValueError("form-fit requires a note")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM part_substitute WHERE part_id = ? AND substitute_part_id = ?",
            (part_id, substitute_part_id),
        ).fetchone()
        if existing is not None:
            raise DuplicateSubstituteError(part_id, substitute_part_id)
        cur = conn.execute(
            "INSERT INTO part_substitute (part_id, substitute_part_id, fit, note, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (part_id, substitute_part_id, fit, note, created_by),
        )
        assert cur.lastrowid is not None
        link_id = int(cur.lastrowid)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    link = get_link(conn, link_id)
    assert link is not None
    return link


def remove_link(conn: sqlite3.Connection, part_id: int, substitute_part_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM part_substitute WHERE part_id = ? AND substitute_part_id = ?",
        (part_id, substitute_part_id),
    )
    conn.commit()
    return cur.rowcount > 0


def suggestions(conn: sqlite3.Connection, part_id: int) -> list[dict]:
    """Parts not yet linked that share *part_id*'s package and value_norm.

    A candidate must have both a package and a value_norm, and must match both
    of the calling part's. Already-linked parts (either direction) and the part
    itself are excluded.
    """
    rows = conn.execute(
        "SELECT p.*, m.name AS manufacturer, m.website AS manufacturer_website, "
        "COALESCE((SELECT SUM(quantity) FROM stock_item WHERE part_id = p.id), 0) AS on_hand "
        "FROM part p JOIN manufacturer m ON m.id = p.manufacturer_id "
        "WHERE p.id != ? "
        "AND p.value_norm IS NOT NULL AND p.value_norm != '' "
        "AND p.package IS NOT NULL AND p.package != '' "
        "AND p.value_norm = (SELECT value_norm FROM part WHERE id = ?) "
        "AND p.package = (SELECT package FROM part WHERE id = ?) "
        "AND p.id NOT IN ("
        "  SELECT substitute_part_id FROM part_substitute WHERE part_id = ? "
        "  UNION "
        "  SELECT part_id FROM part_substitute WHERE substitute_part_id = ?"
        ") "
        "ORDER BY on_hand DESC, p.ipn",
        (part_id, part_id, part_id, part_id, part_id),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "ipn": row["ipn"],
            "mpn": row["mpn"],
            "mpn_norm": row["mpn_norm"],
            "manufacturer": row["manufacturer"],
            "package": row["package"],
            "value": row["value"],
            "value_norm": row["value_norm"],
            "on_hand": int(row["on_hand"]),
        }
        for row in rows
    ]


def shortfall(conn: sqlite3.Connection, part_id: int, required: int) -> dict:
    """The numbers that matter for one BOM line.

    ``short`` is ``max(0, required - on_hand)``; each substitute carries its own
    ``on_hand`` (from the stock ledger) and whether it covers the shortfall.
    """
    on_hand = stock_repo.total_on_hand(conn, part_id)
    short = max(0, required - on_hand)
    substitutes = list_out(conn, part_id)
    for sub in substitutes:
        sub["covers"] = short > 0 and int(sub["substitute_on_hand"]) >= short
    return {
        "required": required,
        "on_hand": on_hand,
        "short": short,
        "substitutes": substitutes,
    }
