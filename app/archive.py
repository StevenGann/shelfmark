"""Export, restore and scheduled backup as a single portable zip.

The database is carried as data — JSON rows keyed by column name — rather than a
raw ``.db`` file copy, because copying a live SQLite file is unsafe (the WAL is a
separate file that may hold the most recent writes) and a plain file cannot be
checksummed, inspected, or refused before it is loaded. Assets are stored under
their existing relative paths so a restore writes them back without translation.

Restore is one transaction: it deletes the target's rows in the bundle's tables,
inserts every table parents-before-children, resets autoincrement counters, then
verifies foreign keys and per-table row counts before committing. A bundle that
fails any check is refused before it is half-loaded, so a failed restore leaves
the previous data in place.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, assets
from .config import settings

FORMAT = 1
# Restore order: parents before children, so every foreign key points at a row
# that has already been inserted.
TABLES: tuple[str, ...] = (
    "app_user",
    "api_key",
    "distributor",
    "manufacturer",
    "part",
    "part_link",
    "part_asset",
    "part_substitute",
    "offer",
    "price_break",
    "project",
    "bom_revision",
    "bom_line",
    "build",
    "build_line",
    "stock_item",
    "stock_move",
    "placement",
    "machine",
    "fetch_job",
    "ipn_counter",
)
EXCLUDED = frozenset({"user_session", "schema_migration"})

# A target counts as "populated" when any of these carry a row; anything else
# (seeded distributors, the machine row, the IPN counter) is scaffolding that a
# fresh instance already has, not data.
_POPULATED_CHECK = ("part", "project", "app_user", "stock_item")


class ArchiveError(Exception):
    """A bundle could not be written, read, or restored."""


def utc_stamp() -> str:
    """The one timestamp format used across manifests and filenames."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    # Read from the live schema, never a hardcoded list, so a future migration
    # adds a column to the bundle instead of silently dropping it.
    return [row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _asset_entries() -> tuple[list[tuple[str, Path]], int, int]:
    entries: list[tuple[str, Path]] = []
    total_bytes = 0
    root = settings.asset_dir
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                entries.append((path.relative_to(root).as_posix(), path))
                total_bytes += path.stat().st_size
    return entries, len(entries), total_bytes


def _gather(
    conn: sqlite3.Connection, *, assets: bool, created_at: str
) -> tuple[dict, bytes, list[tuple[str, Path]]]:
    tables: dict[str, list[dict]] = {}
    counts: dict[str, int] = {}
    for name in TABLES:
        columns = _columns(conn, name)
        select = ", ".join(f'"{column}"' for column in columns)
        rows = [dict(row) for row in conn.execute(f"SELECT {select} FROM {name} ORDER BY id")]
        tables[name] = rows
        counts[name] = len(rows)

    data = json.dumps(
        {"format": FORMAT, "tables": tables}, ensure_ascii=False, separators=(",", ":")
    ).encode()

    if assets:
        asset_entries, asset_files, asset_bytes = _asset_entries()
    else:
        asset_entries, asset_files, asset_bytes = [], 0, 0

    manifest = {
        "format": FORMAT,
        "app": "shelfmark",
        "version": __version__,
        "created_at": created_at,
        "counts": counts,
        "data_sha256": hashlib.sha256(data).hexdigest(),
        "asset_files": asset_files,
        "asset_bytes": asset_bytes,
    }
    return manifest, data, asset_entries


def write_bundle(
    zf: zipfile.ZipFile, conn: sqlite3.Connection, *, assets: bool, created_at: str
) -> dict:
    """Write the manifest, data and assets into an open zip; return the manifest."""
    manifest, data, asset_entries = _gather(conn, assets=assets, created_at=created_at)
    zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    zf.writestr("data.json", data)
    for rel_path, path in asset_entries:
        zf.write(path, f"assets/{rel_path}")
    return manifest


def export_bundle(dest: Path, conn: sqlite3.Connection, *, assets: bool = True) -> dict:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        return write_bundle(zf, conn, assets=assets, created_at=utc_stamp())


def _prune(directory: Path, keep: int) -> None:
    bundles = sorted(directory.glob("shelfmark-*.zip"))
    excess = bundles[:-keep] if keep > 0 else bundles
    for path in excess:
        path.unlink()


def backup_dir(
    directory: Path, conn: sqlite3.Connection, *, keep: int = 14, assets: bool = True
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    dest = directory / f"shelfmark-{stamp}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        write_bundle(zf, conn, assets=assets, created_at=stamp)
    # Prove the bundle we just wrote is readable before claiming success.
    try:
        with zipfile.ZipFile(dest, "r") as zf:
            json.loads(zf.read("manifest.json"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError) as error:
        raise ArchiveError(f"bundle could not be re-read: {error}") from error
    _prune(directory, keep)
    return dest


def _check_empty(conn: sqlite3.Connection, force: bool) -> None:
    if force:
        return
    for name in _POPULATED_CHECK:
        row = conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        if row is not None:
            raise ArchiveError(
                f"target database is not empty (rows in {name}); pass --force to overwrite"
            )


def _has_autoincrement(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return bool(row) and "AUTOINCREMENT" in (row["sql"] or "").upper()


def _reset_sequence(conn: sqlite3.Connection, table: str) -> None:
    if not _has_autoincrement(conn, table):
        return
    row = conn.execute(f'SELECT MAX(id) AS max_id FROM "{table}"').fetchone()
    max_id = row["max_id"]
    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    if max_id is not None:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, int(max_id)))


def _restore_rows(conn: sqlite3.Connection, tables: dict, counts: dict) -> dict[str, int]:
    restored: dict[str, int] = {}
    conn.execute("BEGIN")
    try:
        # Children before parents, so clearing a table never trips a foreign key.
        for name in reversed(TABLES):
            conn.execute(f"DELETE FROM {name}")

        for name in TABLES:
            rows = tables.get(name, [])
            restored[name] = len(rows)
            if not rows:
                continue
            columns = list(rows[0].keys())
            col_list = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            for row in rows:
                conn.execute(
                    f"INSERT INTO {name} ({col_list}) VALUES ({placeholders})",
                    [row.get(column) for column in columns],
                )

        for name in TABLES:
            _reset_sequence(conn, name)

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ArchiveError(f"restore failed the foreign-key check ({len(violations)} row(s))")

        for name in TABLES:
            actual = int(conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"])
            expected = int(counts.get(name, 0))
            if actual != expected:
                raise ArchiveError(
                    f"count mismatch for {name}: expected {expected}, restored {actual}"
                )

        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return restored


def _restore_assets(zf: zipfile.ZipFile, names: list[str]) -> int:
    count = 0
    for name in names:
        rel_path = name[len("assets/") :]
        if assets.resolve(rel_path) is None:
            raise ArchiveError(f"bundle asset path escapes the asset root: {rel_path}")
        tmp = assets.tmp_path(rel_path)
        tmp.write_bytes(zf.read(name))
        assets.commit(tmp, rel_path)
        count += 1
    return count


def restore_bundle(
    src: Path, conn: sqlite3.Connection, *, force: bool = False, assets: bool = True
) -> dict:
    try:
        zf = zipfile.ZipFile(src, "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ArchiveError(f"{src} is not a readable bundle: {error}") from error
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except (json.JSONDecodeError, KeyError) as error:
            raise ArchiveError("manifest.json is missing or unreadable") from error

        if manifest.get("format") != FORMAT:
            raise ArchiveError(f"unsupported bundle format: {manifest.get('format')!r}")
        if manifest.get("app") != "shelfmark":
            raise ArchiveError(f"bundle is not a shelfmark export: {manifest.get('app')!r}")

        data = zf.read("data.json")
        actual = hashlib.sha256(data).hexdigest()
        expected = manifest.get("data_sha256")
        if actual != expected:
            raise ArchiveError(f"checksum mismatch: manifest {expected}, data {actual}")

        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise ArchiveError("data.json is not valid JSON") from error
        tables = payload.get("tables")
        if not isinstance(tables, dict):
            raise ArchiveError("data.json has no tables")

        _check_empty(conn, force)
        restored = _restore_rows(conn, tables, manifest.get("counts") or {})

        asset_names = [
            name
            for name in zf.namelist()
            if name.startswith("assets/") and not name.endswith("/") and len(name) > len("assets/")
        ]
        restored_assets = _restore_assets(zf, asset_names) if assets else 0

    return {"tables": restored, "assets": restored_assets}
