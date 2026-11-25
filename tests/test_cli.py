"""The break-glass path: account management without the web UI."""

from __future__ import annotations

import io
import sys

from app import cli, db, users


def _run(monkeypatch, argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli.main(argv)


def test_create_then_list(monkeypatch, capsys):
    code = _run(monkeypatch, ["user", "create", "alice", "--role", "engineer"], "correct-horse-9\n")
    assert code == 0

    code = _run(monkeypatch, ["user", "list"])
    assert code == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "engineer" in out


def test_short_password_is_refused(monkeypatch, capsys):
    code = _run(monkeypatch, ["user", "create", "bob", "--role", "viewer"], "short\n")
    assert code == 1
    assert "12" in capsys.readouterr().err


def test_passwd_changes_the_password_and_ends_sessions(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "carol", "--role", "admin"], "original-pass-1\n")
    conn = db.connect()
    user = users.get_user_by_username(conn, "carol")
    from app import sessions

    sessions.start(conn, int(user["id"]))
    conn.close()

    code = _run(monkeypatch, ["user", "passwd", "carol"], "replacement-pass-2\n")
    assert code == 0

    conn = db.connect()
    fresh = users.get_user_by_username(conn, "carol")
    sessions_left = conn.execute(
        "select count(*) from user_session where user_id = ?", (int(user["id"]),)
    ).fetchone()[0]
    conn.close()

    assert users.verify_password(fresh["password_hash"], "replacement-pass-2")
    assert not users.verify_password(fresh["password_hash"], "original-pass-1")
    assert sessions_left == 0


def test_last_admin_cannot_be_disabled_or_demoted(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "root-one", "--role", "admin"], "root-one-pass-1\n")

    code = _run(monkeypatch, ["user", "disable", "root-one"])
    assert code == 1
    assert "last active admin" in capsys.readouterr().err

    code = _run(monkeypatch, ["user", "role", "root-one", "viewer"])
    assert code == 1
    assert "last active admin" in capsys.readouterr().err

    conn = db.connect()
    assert users.get_user_by_username(conn, "root-one")["role"] == "admin"
    conn.close()


def test_demoting_one_of_two_admins_is_allowed(monkeypatch, capsys):
    _run(monkeypatch, ["user", "create", "admin-a", "--role", "admin"], "admin-a-pass-11\n")
    _run(monkeypatch, ["user", "create", "admin-b", "--role", "admin"], "admin-b-pass-11\n")

    code = _run(monkeypatch, ["user", "role", "admin-b", "engineer"])
    assert code == 0

    conn = db.connect()
    assert users.get_user_by_username(conn, "admin-b")["role"] == "engineer"
    conn.close()


# --- the rest of the CLI ----------------------------------------------------
#
# The account commands above are the break-glass path. Everything else the
# container ships is exercised here too, so a broken subcommand fails CI rather
# than the night someone needs it.

import json  # noqa: E402

import zxingcpp  # noqa: E402
from PIL import Image  # noqa: E402

BOM = b"Reference,Qty,Value,Footprint,MPN,Manufacturer\nR1,1,10k,0402,RC0402FR-0710KL,Yageo\n"


def _call(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _part(mpn: str = "RC0402FR-0710KL", manufacturer: str = "Yageo") -> dict:
    from app.repo import parts as repo

    conn = db.connect()
    try:
        return repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
    finally:
        conn.close()


def _label_image(payload: str) -> bytes:
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.DataMatrix)
    raw = barcode.to_image()
    height, width = raw.shape
    image = Image.frombytes("L", (width, height), bytes(raw)).convert("RGB")
    image = image.resize((width * 8, height * 8), Image.Resampling.NEAREST)
    pad = Image.new("RGB", (image.width + 80, image.height + 80), "white")
    pad.paste(image, (40, 40))
    out = io.BytesIO()
    pad.save(out, format="PNG")
    return out.getvalue()


OPA211 = (
    "[)>\x1e06\x1dP296-OPA211AIDRGRCT-ND\x1d1POPA211AIDRGR\x1d30P296-OPA211AIDRGRCT-ND"
    "\x1dKCAN AND THERMOMETER\x1d9D2524+5\x1d1T5397196ML5\x1d4LDE\x1dQ25\x1d11ZPICK"
)


def test_key_create_list_and_revoke(capsys):
    from uuid import uuid4

    name = f"cli-{uuid4().hex[:8]}"

    code, out, _ = _call(["key", "create", "--name", name, "--role", "engineer"], capsys)
    assert code == 0
    assert out.strip().startswith("smk_")

    code, out, _ = _call(["key", "list"], capsys)
    assert code == 0
    assert name in out and "engineer" in out

    assert _call(["key", "revoke", "--name", name], capsys)[0] == 0

    conn = db.connect()
    try:
        row = conn.execute("SELECT revoked_at FROM api_key WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    assert row["revoked_at"] is not None


def test_bom_import_creates_the_project_and_lists_it(tmp_path, capsys):
    path = tmp_path / "BOM.csv"
    path.write_bytes(BOM)
    _part()

    code, out, _ = _call(
        ["bom", "import", "--project", "widget", "--label", "rev A", "--file", str(path)], capsys
    )
    assert code == 0
    assert "matched" in out

    code, out, _ = _call(["bom", "list", "--project", "widget"], capsys)
    assert code == 0
    assert "rev A" in out

    code, _, err = _call(["bom", "list", "--project", "nope"], capsys)
    assert code == 1
    assert "no such project" in err


def test_a_build_is_planned_committed_and_exported(tmp_path, capsys):
    from app.repo import boms as boms_repo
    from app.repo import stock as stock_repo

    part = _part()
    conn = db.connect()
    try:
        stock_repo.record_move(conn, int(part["id"]), "Bin 1", "receive", 50, None, "t")
        project = boms_repo.create_project(conn, name="widget")
        revision = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(revision["revision_id"])
    finally:
        conn.close()

    code, out, _ = _call(["build", "plan", str(revision_id), "--boards", "5"], capsys)
    assert code == 0
    assert "planned build" in out
    build_id = int(out.split("planned build ")[1].split(" ")[0])

    code, out, _ = _call(["build", "commit", str(build_id)], capsys)
    assert code == 0
    assert "committed build" in out

    code, out, _ = _call(["build", "list"], capsys)
    assert code == 0
    assert "committed" in out

    outdir = tmp_path / "export"
    code, out, _ = _call(["build", "export", str(build_id), "--out", str(outdir)], capsys)
    assert code == 0
    assert (outdir / "picklist.csv").exists()
    assert (outdir / "job.xml").exists()
    # No positions were imported, so the board file is not invented.
    assert not (outdir / "board.xml").exists()


def test_committing_a_short_build_exits_non_zero(capsys):
    from app.repo import boms as boms_repo

    part = _part()
    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="widget")
        revision = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(revision["revision_id"])
    finally:
        conn.close()

    code, out, _ = _call(["build", "plan", str(revision_id), "--boards", "5"], capsys)
    build_id = int(out.split("planned build ")[1].split(" ")[0])

    code, _, err = _call(["build", "commit", str(build_id)], capsys)
    assert code == 1
    assert "cannot commit" in err
    assert part["ipn"] in err


def test_substitutes_are_linked_listed_and_removed(capsys):
    part = _part("LM358N", "TI")
    alternative = _part("LM358DR2G", "onsemi")
    argv = [
        "subs",
        "add",
        part["ipn"],
        alternative["ipn"],
        "--fit",
        "equivalent",
        "--note",
        "same die",
    ]

    code, out, _ = _call(argv, capsys)
    assert code == 0
    assert "linked" in out

    code, out, _ = _call(["subs", "list", part["ipn"]], capsys)
    assert code == 0
    assert alternative["ipn"] in out

    code, _, err = _call(argv, capsys)
    assert code == 1
    assert "already exists" in err

    code, out, _ = _call(["subs", "remove", part["ipn"], alternative["ipn"]], capsys)
    assert code == 0
    assert "removed" in out


def test_unknown_entities_are_reported_not_crashed(capsys):
    code, _, err = _call(["user", "disable", "nobody"], capsys)
    assert code == 1
    assert "no such user" in err

    code, _, err = _call(["subs", "list", "SK-99999"], capsys)
    assert code == 1
    assert "no such part" in err

    code, _, err = _call(["enrich", "--ipn", "SK-99999"], capsys)
    assert code == 1
    assert "no such part" in err


def test_offer_audit_reports_a_clean_ladder(capsys):
    code, out, _ = _call(["offers", "audit"], capsys)
    assert code == 0
    assert "no inverted price breaks" in out


def test_offer_refresh_without_credentials_fails_loudly(capsys):
    part = _part()
    code, _, err = _call(["offers", "refresh", "--ipn", part["ipn"]], capsys)
    assert code == 1
    assert "refresh failed" in err


def test_label_scan_prints_the_parsed_label(tmp_path, capsys):
    path = tmp_path / "label.png"
    path.write_bytes(_label_image(OPA211))

    code, out, _ = _call(["label", "scan", "--file", str(path)], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["label"]["mpn"] == "OPA211AIDRGR"
    assert payload["label"]["quantity"] == 25


def test_label_receive_creates_the_part_and_records_stock(tmp_path, capsys):
    path = tmp_path / "label.png"
    path.write_bytes(_label_image(OPA211))

    code, out, _ = _call(
        [
            "label",
            "receive",
            "--file",
            str(path),
            "--location",
            "Bin 3",
            "--manufacturer",
            "Texas Instruments",
        ],
        capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["part"]["ipn"].startswith("SK-")
    assert payload["move"]["quantity"] == 25
    assert payload["move"]["location"] == "Bin 3"


def test_a_missing_label_file_is_reported(tmp_path, capsys):
    code, _, err = _call(["label", "scan", "--file", str(tmp_path / "nope.png")], capsys)
    assert code == 1
    assert "cannot read" in err


def test_enrich_all_runs_the_queue(capsys):
    _part()
    code, out, _ = _call(["enrich", "--all"], capsys)
    assert code == 0
    assert "processed" in out
