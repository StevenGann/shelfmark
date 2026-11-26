"""M7: export, restore and scheduled backup."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app import archive, db
from app.config import settings
from app.repo import boms as boms_repo
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo

BOM = b"Reference,Qty,Value,Footprint,MPN,Manufacturer\nR1,1,10k,0402,RC0402FR-0710KL,Yageo\n"


def _seed_source() -> dict:
    """One part with stock, one project with a BOM revision, and an asset file."""
    conn = db.connect()
    try:
        part = parts_repo.create_part(conn, mpn="RC0402FR-0710KL", manufacturer="Yageo")
        stock_repo.record_move(conn, int(part["id"]), "Bin 1", "receive", 42, None, "test")
        project = boms_repo.create_project(conn, name="widget")
        boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="test",
        )
    finally:
        conn.close()
    asset = settings.asset_dir / "parts" / part["ipn"] / "image.jpg"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"\xff\xd8\xff\xe0roundtrip")
    return part


def _table_counts(conn) -> dict:
    return {
        name: int(conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"])
        for name in archive.TABLES
    }


def _second_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "second" / "data")
    monkeypatch.setattr(settings, "asset_dir", tmp_path / "second" / "assets")
    db.migrate()
    return db.connect()


def test_export_then_restore_into_second_directory_matches_every_table(monkeypatch, tmp_path):
    _seed_source()

    src_conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        manifest = archive.export_bundle(bundle, src_conn)
    finally:
        src_conn.close()

    conn = _second_instance(monkeypatch, tmp_path)
    try:
        archive.restore_bundle(bundle, conn)
        assert _table_counts(conn) == manifest["counts"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_asset_survives_the_round_trip(monkeypatch, tmp_path):
    part = _seed_source()
    rel_path = f"parts/{part['ipn']}/image.jpg"

    src_conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        archive.export_bundle(bundle, src_conn)
    finally:
        src_conn.close()

    conn = _second_instance(monkeypatch, tmp_path)
    try:
        archive.restore_bundle(bundle, conn)
    finally:
        conn.close()

    restored = settings.asset_dir / rel_path
    assert restored.read_bytes() == b"\xff\xd8\xff\xe0roundtrip"


def test_tampered_data_json_is_refused(tmp_path):
    conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        archive.export_bundle(bundle, conn)
    finally:
        conn.close()

    _rewrite_entry(bundle, "data.json", b'{"format": 1, "tables": {"app_user": []}}')

    conn = db.connect()
    try:
        with pytest.raises(archive.ArchiveError, match="checksum mismatch"):
            archive.restore_bundle(bundle, conn)
    finally:
        conn.close()


def test_restore_into_populated_database_is_refused(tmp_path):
    _seed_source()
    conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        archive.export_bundle(bundle, conn)
        with pytest.raises(archive.ArchiveError, match="not empty"):
            archive.restore_bundle(bundle, conn)
    finally:
        conn.close()


def test_backup_keep_two_leaves_two_bundles(monkeypatch, tmp_path):
    conn = db.connect()
    try:
        stamps = iter(["2026-11-26T03:17:00Z", "2026-11-26T03:18:00Z", "2026-11-26T03:19:00Z"])
        monkeypatch.setattr(archive, "utc_stamp", lambda: next(stamps))
        for _ in range(3):
            archive.backup_dir(tmp_path, conn, keep=2)
    finally:
        conn.close()

    bundles = sorted(bundle.name for bundle in tmp_path.glob("shelfmark-*.zip"))
    assert bundles == [
        "shelfmark-2026-11-26T03:18:00Z.zip",
        "shelfmark-2026-11-26T03:19:00Z.zip",
    ]


def test_api_export_is_403_for_viewer_and_zip_for_admin(anon_client, client, make_key):
    viewer = make_key("viewer")
    r = anon_client.get("/api/export", headers={"X-API-Key": viewer})
    assert r.status_code == 403

    r = client.get("/api/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["cache-control"] == "no-store"
    assert "attachment" in r.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert "manifest.json" in zf.namelist()
        assert "data.json" in zf.namelist()


def test_restore_reproduces_part_bom_and_stock_quantities(monkeypatch, tmp_path):
    _seed_source()

    src_conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        archive.export_bundle(bundle, src_conn)
        source = _source_quantities(src_conn)
    finally:
        src_conn.close()

    conn = _second_instance(monkeypatch, tmp_path)
    try:
        archive.restore_bundle(bundle, conn)
        assert _source_quantities(conn) == source
    finally:
        conn.close()


def _source_quantities(conn) -> dict:
    return {
        "part": int(conn.execute("SELECT count(*) AS n FROM part").fetchone()["n"]),
        "bom_line": int(conn.execute("SELECT count(*) AS n FROM bom_line").fetchone()["n"]),
        "stock_items": int(conn.execute("SELECT count(*) AS n FROM stock_item").fetchone()["n"]),
        "stock_quantity": int(
            conn.execute("SELECT COALESCE(SUM(quantity), 0) AS n FROM stock_item").fetchone()["n"]
        ),
    }


def _rewrite_entry(bundle, name, content: bytes) -> None:
    with zipfile.ZipFile(bundle, "r") as src:
        entries = {n: src.read(n) for n in src.namelist()}
    entries[name] = content
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as dst:
        for entry_name, data in entries.items():
            dst.writestr(entry_name, data)


def test_force_replaces_the_existing_rows(monkeypatch, tmp_path):
    """--force must leave the target byte-for-byte the bundle's database."""
    first = _seed_source()

    src_conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        manifest = archive.export_bundle(bundle, src_conn)
    finally:
        src_conn.close()

    # Diverge the source: a second part that the bundle does not carry.
    conn = db.connect()
    try:
        parts_repo.create_part(conn, mpn="SECOND-PART", manufacturer="Yageo")
        conn.close()

        conn = db.connect()
        assert conn.execute("SELECT count(*) AS n FROM part").fetchone()["n"] == 2
        archive.restore_bundle(bundle, conn, force=True)
        assert _table_counts(conn) == manifest["counts"]
        assert conn.execute("SELECT count(*) AS n FROM part").fetchone()["n"] == 1
        ipn = conn.execute("SELECT ipn FROM part").fetchone()["ipn"]
        assert ipn == first["ipn"]
    finally:
        conn.close()


def test_unknown_bundle_format_is_refused(tmp_path):
    conn = db.connect()
    try:
        bundle = tmp_path / "bundle.zip"
        manifest = archive.export_bundle(bundle, conn)
    finally:
        conn.close()

    manifest["format"] = 99
    with zipfile.ZipFile(bundle, "r") as zf:
        data = zf.read("data.json")
    _rewrite_entry(bundle, "manifest.json", json.dumps(manifest).encode())

    conn = db.connect()
    try:
        with pytest.raises(archive.ArchiveError, match="unsupported bundle format"):
            archive.restore_bundle(bundle, conn)
    finally:
        conn.close()
    assert data  # the data entry survived the manifest rewrite


def test_cli_export_then_restore_into_a_second_directory(monkeypatch, tmp_path, capsys):
    from app import cli

    _seed_source()
    bundle = tmp_path / "cli-bundle.zip"
    assert cli.main(["export", "--out", str(bundle), "--no-assets"]) == 0
    assert capsys.readouterr().out.strip().endswith("bytes")

    conn = _second_instance(monkeypatch, tmp_path)
    try:
        assert cli.main(["restore", str(bundle)]) == 0
        out = capsys.readouterr().out
        assert "part 1 ok" in out
        assert "restored" in out
        assert conn.execute("SELECT count(*) AS n FROM part").fetchone()["n"] == 1
    finally:
        conn.close()


def test_cli_restore_reports_a_bad_bundle(tmp_path, capsys):
    from app import cli

    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    assert cli.main(["restore", str(broken)]) == 1
    assert "not a readable bundle" in capsys.readouterr().err.lower()
