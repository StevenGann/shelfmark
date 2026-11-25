"""Repository functions for offers and their price breaks.

An offer is one buyable packaging/SKU combination from a distributor; a price
break ladder sits under it. A refresh replaces an offer's ladder wholesale, so a
stale break never sits beside a fresh one and a price that exists nowhere cannot
be quoted.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import catalog


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_offer(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "part_id": row["part_id"],
        "distributor_id": row["distributor_id"],
        "distributor": row["distributor"],
        "sku": row["sku"],
        "packaging": row["packaging"],
        "url": row["url"],
        "currency": row["currency"],
        "minimum_quantity": row["minimum_quantity"],
        "stock_quantity": row["stock_quantity"],
        "source": row["source"],
        "fetched_at": row["fetched_at"],
        "created_by": row["created_by"],
        "breaks": [],
    }


def _breaks_for(conn: sqlite3.Connection, offer_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT quantity, unit_price_micros FROM price_break WHERE offer_id = ? ORDER BY quantity",
        (offer_id,),
    ).fetchall()
    return [
        {"quantity": int(r["quantity"]), "unit_price_micros": int(r["unit_price_micros"])}
        for r in rows
    ]


def list_offers(conn: sqlite3.Connection, part_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT o.*, d.name AS distributor FROM offer o "
        "JOIN distributor d ON d.id = o.distributor_id "
        "WHERE o.part_id = ? ORDER BY o.id",
        (part_id,),
    ).fetchall()
    offers: list[dict[str, Any]] = []
    for row in rows:
        offer = _row_to_offer(row)
        offer["breaks"] = _breaks_for(conn, int(offer["id"]))
        offers.append(offer)
    return offers


def get_offer(conn: sqlite3.Connection, offer_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT o.*, d.name AS distributor FROM offer o "
        "JOIN distributor d ON d.id = o.distributor_id WHERE o.id = ?",
        (offer_id,),
    ).fetchone()
    if row is None:
        return None
    offer = _row_to_offer(row)
    offer["breaks"] = _breaks_for(conn, offer_id)
    return offer


def delete_offer(conn: sqlite3.Connection, offer_id: int) -> bool:
    cur = conn.execute("DELETE FROM offer WHERE id = ?", (offer_id,))
    conn.commit()
    return cur.rowcount > 0


def add_manual_offer(
    conn: sqlite3.Connection,
    part_id: int,
    *,
    distributor_id: int,
    sku: str,
    packaging: str | None = None,
    url: str | None = None,
    currency: str = "USD",
    minimum_quantity: int = 1,
    stock_quantity: int | None = None,
    breaks: list[dict[str, Any]],
    created_by: str | None = None,
) -> dict[str, Any]:
    """Insert a hand-entered offer and its ladder in one transaction."""
    if minimum_quantity < 1:
        raise ValueError("minimum_quantity must be at least 1")
    for b in breaks:
        if int(b["quantity"]) < 1:
            raise ValueError("break quantity must be at least 1")
        if int(b["unit_price_micros"]) < 0:
            raise ValueError("unit price must not be negative")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO offer (part_id, distributor_id, sku, packaging, url, currency, "
            "minimum_quantity, stock_quantity, source, fetched_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', NULL, ?)",
            (
                part_id,
                distributor_id,
                sku,
                packaging,
                url,
                currency,
                minimum_quantity,
                stock_quantity,
                created_by,
            ),
        )
        assert cur.lastrowid is not None
        offer_id = int(cur.lastrowid)
        for b in breaks:
            conn.execute(
                "INSERT INTO price_break (offer_id, quantity, unit_price_micros) VALUES (?, ?, ?)",
                (offer_id, int(b["quantity"]), int(b["unit_price_micros"])),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    offer = get_offer(conn, offer_id)
    assert offer is not None
    return offer


def upsert_offers(
    conn: sqlite3.Connection,
    part_id: int,
    source: str,
    offers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace a part's *source* offers with *offers* in one transaction.

    Each incoming offer's ladder is replaced wholesale. Offers of the same
    ``(part, distributor, source)`` that are absent from *offers* are marked
    ``fetched_at`` current with no breaks (they disappeared from the supplier)
    rather than deleted, so their history survives.
    """
    if source != "digikey":
        raise ValueError("upsert_offers only supports source='digikey'")
    distributor = catalog.get_distributor_by_name_norm(conn, "digikey")
    if distributor is None:
        raise ValueError("digikey distributor row is missing")
    distributor_id = int(distributor["id"])
    now = _utc_now()

    conn.execute("BEGIN IMMEDIATE")
    try:
        incoming_skus = {str(o["sku"]) for o in offers}
        for offer in offers:
            sku = str(offer["sku"])
            packaging = offer.get("packaging")
            url = offer.get("url")
            minimum_quantity = int(offer.get("minimum_quantity", 1))
            stock_quantity = offer.get("stock_quantity")
            existing = conn.execute(
                "SELECT id FROM offer WHERE part_id = ? AND distributor_id = ? AND sku = ?",
                (part_id, distributor_id, sku),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO offer (part_id, distributor_id, sku, packaging, url, currency, "
                    "minimum_quantity, stock_quantity, source, fetched_at, created_by) "
                    "VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, ?, ?, NULL)",
                    (
                        part_id,
                        distributor_id,
                        sku,
                        packaging,
                        url,
                        minimum_quantity,
                        stock_quantity,
                        source,
                        now,
                    ),
                )
                assert cur.lastrowid is not None
                offer_id = int(cur.lastrowid)
            else:
                offer_id = int(existing["id"])
                conn.execute(
                    "UPDATE offer SET packaging = ?, url = ?, minimum_quantity = ?, "
                    "stock_quantity = ?, fetched_at = ? WHERE id = ?",
                    (packaging, url, minimum_quantity, stock_quantity, now, offer_id),
                )
            conn.execute("DELETE FROM price_break WHERE offer_id = ?", (offer_id,))
            for b in offer.get("breaks", []):
                conn.execute(
                    "INSERT INTO price_break (offer_id, quantity, unit_price_micros) "
                    "VALUES (?, ?, ?)",
                    (offer_id, int(b["quantity"]), int(b["unit_price_micros"])),
                )

        if incoming_skus:
            placeholders = ",".join("?" for _ in incoming_skus)
            gone = conn.execute(
                f"SELECT id FROM offer WHERE part_id = ? AND distributor_id = ? "
                f"AND source = ? AND sku NOT IN ({placeholders})",
                (part_id, distributor_id, source, *incoming_skus),
            ).fetchall()
        else:
            gone = conn.execute(
                "SELECT id FROM offer WHERE part_id = ? AND distributor_id = ? AND source = ?",
                (part_id, distributor_id, source),
            ).fetchall()
        for row in gone:
            offer_id = int(row["id"])
            conn.execute("DELETE FROM price_break WHERE offer_id = ?", (offer_id,))
            conn.execute("UPDATE offer SET fetched_at = ? WHERE id = ?", (now, offer_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return list_offers(conn, part_id)
