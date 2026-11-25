"""Repository functions for the enrichment side of the catalogue.

Manufacturers, distributors, part links, local assets and the fetch-job queue
all live here. Like ``parts.py`` there is no ORM: each function takes a
connection, and the write helpers own their transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

DATASHEET_KIND = "datasheet"
IMAGE_KIND = "image"

LINK_KINDS = ("datasheet", "manufacturer", "distributor")
JOB_KINDS = ("enrich",)
JOB_STATUSES = ("queued", "running", "done", "failed")


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalise_name(name: str) -> str:
    """The canonical key for a manufacturer or distributor name.

    Lower-case and trimmed, matching the rule the migration uses to backfill
    ``manufacturer.name_norm``.
    """
    return name.strip().lower()


def _ensure_manufacturer(conn: sqlite3.Connection, name: str) -> int:
    """Return the id for *name*, inserting it if absent. Does not commit."""
    name = name.strip()
    name_norm = normalise_name(name)
    row = conn.execute("SELECT id FROM manufacturer WHERE name_norm = ?", (name_norm,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO manufacturer (name, name_norm) VALUES (?, ?)", (name, name_norm)
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def upsert_manufacturer(
    conn: sqlite3.Connection, name: str, website: str | None = None
) -> dict[str, Any]:
    """Insert or fetch a manufacturer, optionally setting its website."""
    manufacturer_id = _ensure_manufacturer(conn, name)
    if website is not None:
        conn.execute("UPDATE manufacturer SET website = ? WHERE id = ?", (website, manufacturer_id))
    conn.commit()
    row = get_manufacturer(conn, manufacturer_id)
    assert row is not None
    return row


def get_manufacturer(conn: sqlite3.Connection, manufacturer_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM manufacturer WHERE id = ?", (manufacturer_id,)).fetchone()
    return None if row is None else _row_to_manufacturer(row)


def _row_to_manufacturer(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "name_norm": row["name_norm"],
        "website": row["website"],
    }


def list_distributors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM distributor ORDER BY id").fetchall()
    return [_row_to_distributor(r) for r in rows]


def get_distributor(conn: sqlite3.Connection, distributor_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM distributor WHERE id = ?", (distributor_id,)).fetchone()
    return None if row is None else _row_to_distributor(row)


def get_distributor_by_name_norm(conn: sqlite3.Connection, name_norm: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM distributor WHERE name_norm = ?", (name_norm,)).fetchone()
    return None if row is None else _row_to_distributor(row)


def _row_to_distributor(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "name_norm": row["name_norm"],
        "search_url_template": row["search_url_template"],
        "product_url_template": row["product_url_template"],
    }


def add_part_link(
    conn: sqlite3.Connection,
    part_id: int,
    *,
    kind: str,
    label: str,
    url: str,
    distributor_id: int | None = None,
    sku: str | None = None,
    source: str,
    fetched_at: str | None = None,
) -> bool:
    """Insert a part link, ignoring it when the same (kind, url) exists."""
    if kind not in LINK_KINDS:
        raise ValueError(f"invalid link kind: {kind}")
    cur = conn.execute(
        """
        INSERT INTO part_link (part_id, kind, label, url, distributor_id, sku, source, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (part_id, kind, url) DO NOTHING
        """,
        (part_id, kind, label, url, distributor_id, sku, source, fetched_at),
    )
    conn.commit()
    return cur.rowcount > 0


def list_part_links(conn: sqlite3.Connection, part_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM part_link WHERE part_id = ? ORDER BY id", (part_id,)
    ).fetchall()
    return [_row_to_link(r) for r in rows]


def _row_to_link(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "part_id": row["part_id"],
        "kind": row["kind"],
        "label": row["label"],
        "url": row["url"],
        "distributor_id": row["distributor_id"],
        "sku": row["sku"],
        "source": row["source"],
        "fetched_at": row["fetched_at"],
    }


def add_part_asset(
    conn: sqlite3.Connection,
    part_id: int,
    *,
    kind: str,
    source_url: str,
    rel_path: str,
    content_type: str,
    bytes: int,
    sha256: str,
    fetched_at: str,
    status: str,
    error: str | None = None,
    source: str = "digikey",
    uploaded_by: str | None = None,
    original_filename: str | None = None,
) -> bool:
    """Record an asset result, upserting on (part, kind, source_url)."""
    if kind not in (IMAGE_KIND, DATASHEET_KIND):
        raise ValueError(f"invalid asset kind: {kind}")
    if status not in ("ok", "failed"):
        raise ValueError(f"invalid asset status: {status}")
    conn.execute(
        """
        INSERT INTO part_asset (
          part_id, kind, source_url, rel_path, content_type, bytes, sha256,
          fetched_at, status, error, source, uploaded_by, original_filename
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (part_id, kind, source_url) DO UPDATE SET
          rel_path = excluded.rel_path,
          content_type = excluded.content_type,
          bytes = excluded.bytes,
          sha256 = excluded.sha256,
          fetched_at = excluded.fetched_at,
          status = excluded.status,
          error = excluded.error,
          source = excluded.source,
          uploaded_by = excluded.uploaded_by,
          original_filename = excluded.original_filename
        """,
        (
            part_id,
            kind,
            source_url,
            rel_path,
            content_type,
            bytes,
            sha256,
            fetched_at,
            status,
            error,
            source,
            uploaded_by,
            original_filename,
        ),
    )
    conn.commit()
    return True


def delete_part_assets_of_kind(conn: sqlite3.Connection, part_id: int, kind: str) -> None:
    """Drop recorded assets of one kind, so a refetch leaves exactly one row."""
    conn.execute("DELETE FROM part_asset WHERE part_id = ? AND kind = ?", (part_id, kind))
    conn.commit()


def list_part_assets(conn: sqlite3.Connection, part_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM part_asset WHERE part_id = ? ORDER BY id", (part_id,)
    ).fetchall()
    return [_row_to_asset(r) for r in rows]


def get_part_asset_by_rel_path(conn: sqlite3.Connection, rel_path: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM part_asset WHERE rel_path = ?", (rel_path,)).fetchone()
    return None if row is None else _row_to_asset(row)


def _row_to_asset(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "part_id": row["part_id"],
        "kind": row["kind"],
        "source_url": row["source_url"],
        "rel_path": row["rel_path"],
        "content_type": row["content_type"],
        "bytes": row["bytes"],
        "sha256": row["sha256"],
        "fetched_at": row["fetched_at"],
        "status": row["status"],
        "error": row["error"],
        "source": row["source"],
        "uploaded_by": row["uploaded_by"],
        "original_filename": row["original_filename"],
    }


def get_part_asset(conn: sqlite3.Connection, part_id: int, kind: str) -> dict[str, Any] | None:
    """Return the current (most recently recorded) asset of *kind*, if any."""
    row = conn.execute(
        "SELECT * FROM part_asset WHERE part_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (part_id, kind),
    ).fetchone()
    return None if row is None else _row_to_asset(row)


def thumbnails(conn: sqlite3.Connection, part_ids: list[int]) -> dict[int, str]:
    """Map part id -> image rel_path for parts with a successfully fetched image."""
    if not part_ids:
        return {}
    placeholders = ",".join("?" for _ in part_ids)
    rows = conn.execute(
        f"SELECT part_id, rel_path FROM part_asset "
        f"WHERE kind = 'image' AND status = 'ok' AND part_id IN ({placeholders})",
        part_ids,
    ).fetchall()
    return {int(r["part_id"]): r["rel_path"] for r in rows}


def queue_job(conn: sqlite3.Connection, part_id: int, *, kind: str = "enrich") -> int:
    """Create a queued fetch job and return its id."""
    if kind not in JOB_KINDS:
        raise ValueError(f"invalid job kind: {kind}")
    now = _utc_now()
    cur = conn.execute(
        """
        INSERT INTO fetch_job (part_id, kind, status, attempts, next_attempt_at, created_at)
        VALUES (?, ?, 'queued', 0, ?, ?)
        """,
        (part_id, kind, now, now),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM fetch_job WHERE id = ?", (job_id,)).fetchone()
    return None if row is None else _row_to_job(row)


def requeue_stale_running(conn: sqlite3.Connection) -> int:
    """Return jobs stuck in "running" to the queue; returns how many moved."""
    cursor = conn.execute("UPDATE fetch_job SET status = 'queued' WHERE status = 'running'")
    conn.commit()
    return cursor.rowcount


def list_due_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM fetch_job WHERE status = 'queued' AND next_attempt_at <= ? ORDER BY id",
        (_utc_now(),),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def latest_job(conn: sqlite3.Connection, part_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM fetch_job WHERE part_id = ? ORDER BY id DESC LIMIT 1", (part_id,)
    ).fetchone()
    return None if row is None else _row_to_job(row)


def has_pending_job(conn: sqlite3.Connection, part_id: int, kind: str = "enrich") -> bool:
    row = conn.execute(
        "SELECT 1 FROM fetch_job WHERE part_id = ? AND kind = ? AND status IN ('queued','running')",
        (part_id, kind),
    ).fetchone()
    return row is not None


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "part_id": row["part_id"],
        "kind": row["kind"],
        "status": row["status"],
        "attempts": row["attempts"],
        "next_attempt_at": row["next_attempt_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }


def mark_running(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE fetch_job SET status = 'running', attempts = attempts + 1 WHERE id = ?",
        (job_id,),
    )
    conn.commit()


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE fetch_job SET status = 'done', finished_at = ?, last_error = NULL WHERE id = ?",
        (_utc_now(), job_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    conn.execute(
        "UPDATE fetch_job SET status = 'failed', finished_at = ?, last_error = ? WHERE id = ?",
        (_utc_now(), error, job_id),
    )
    conn.commit()


def mark_retry(conn: sqlite3.Connection, job_id: int, error: str, next_attempt_at: str) -> None:
    conn.execute(
        "UPDATE fetch_job SET status = 'queued', next_attempt_at = ?, last_error = ? WHERE id = ?",
        (next_attempt_at, error, job_id),
    )
    conn.commit()
