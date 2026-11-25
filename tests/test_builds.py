"""Builds and the OpenPnP handoff (M6)."""

import csv
import io
import xml.etree.ElementTree as ET

import pytest

from app import db
from app.positions import PositionError, parse_positions
from app.repo import builds as builds_repo
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo
from app.repo import substitutes as subs_repo


def _part(mpn, manufacturer):
    conn = db.connect()
    try:
        return parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
    finally:
        conn.close()


def _receive(part, location, quantity):
    conn = db.connect()
    try:
        stock_repo.record_move(conn, int(part["id"]), location, "receive", quantity, None, "t")
    finally:
        conn.close()


def _csv(headers, rows, delimiter=","):
    out = [delimiter.join(headers)]
    for row in rows:
        out.append(delimiter.join(str(c) for c in row))
    return "\n".join(out).encode()


def _pos_csv(headers, rows):
    return _csv(headers, rows)


def _project(client, name="sk-sensor"):
    return client.post("/api/projects", json={"name": name}).json()


def _import(client, project_id, label, data):
    return client.post(
        f"/api/projects/{project_id}/boms",
        data={"label": label},
        files={"file": ("BOM.csv", data, "text/csv")},
    )


def _plan(client, project_id, revision_id, boards, note=None):
    return client.post(
        f"/api/projects/{project_id}/boms/{revision_id}/builds",
        json={"boards": boards, "note": note},
    )


def _moves(conn, part_id):
    return conn.execute(
        "SELECT sm.* FROM stock_move sm "
        "JOIN stock_item si ON si.id = sm.stock_item_id "
        "WHERE si.part_id = ? ORDER BY sm.id",
        (part_id,),
    ).fetchall()


def test_plan_arithmetic_dnp_unmatched_and_short(client):
    part = client.post(
        "/api/parts", json={"mpn": "RC0402FR-0710KL", "manufacturer": "Yageo"}
    ).json()
    _receive(part, "Bin A", 20)
    project = _project(client)
    data = _csv(
        ["Reference", "Qty", "Value", "Footprint", "MPN", "Manufacturer", "DNP"],
        [
            ["R1 R2 R3", "3", "10k", "0402", "RC0402FR-0710KL", "Yageo", ""],
            ["R9", "1", "1k", "0402", "RC0402FR-07101KL", "Yageo", "DNP"],
            ["U1", "1", "opamp", "SOIC-8", "NE555", "TI", ""],
        ],
    )
    imported = _import(client, project["id"], "rev A", data)
    assert imported.status_code == 201
    revision_id = imported.json()["revision_id"]

    planned = _plan(client, project["id"], revision_id, boards=10)
    assert planned.status_code == 201
    build = planned.json()
    lines = build["lines"]

    by_mpn = {line["mpn"]: line for line in lines}
    assert set(by_mpn) == {"RC0402FR-0710KL", "NE555"}  # DNP line excluded

    matched = by_mpn["RC0402FR-0710KL"]
    assert matched["part_id"] == part["id"]
    assert matched["required"] == 30  # 3 per board x 10 boards
    assert matched["on_hand"] == 20
    assert matched["short"] == 10

    unmatched = by_mpn["NE555"]
    assert unmatched["part_id"] is None
    assert unmatched["required"] == 10
    assert unmatched["short"] == 10


def test_plan_writes_no_stock_rows(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]

    planned = _plan(client, project["id"], revision_id, boards=3)
    assert planned.status_code == 201

    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM stock_move").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM stock_item").fetchone()["n"] == 0
    finally:
        conn.close()


def test_commit_consumes_required_and_keeps_ledger_invariant(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    _receive(part, "Bin A", 15)
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=10).json()["id"]

    committed = client.post(f"/api/builds/{build_id}/commit")
    assert committed.status_code == 200
    body = committed.json()
    assert body["status"] == "committed"
    assert body["lines"][0]["consumed"] == 10

    conn = db.connect()
    try:
        assert stock_repo.total_on_hand(conn, int(part["id"])) == 5
        moves = _moves(conn, int(part["id"]))
        builds = [m for m in moves if m["kind"] == "build"]
        assert sum(m["delta"] for m in builds) == -10
        for item in stock_repo.get_stock(conn, int(part["id"])):
            ledger = sum(
                m["delta"]
                for m in _moves(conn, int(part["id"]))
                if m["stock_item_id"] == item["id"]
            )
            assert item["quantity"] == ledger
    finally:
        conn.close()


def test_commit_counts_two_lines_sharing_a_part_against_one_pile_of_stock(client):
    """Each line fits on its own; together they do not. Neither may half-commit."""
    part = client.post("/api/parts", json={"mpn": "SHARED-1", "manufacturer": "TI"}).json()
    _receive(part, "Bin A", 15)
    project = _project(client, name="shared")
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "SHARED-1", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]

    # A second line for the same part: a second footprint of the same device, or
    # the same part listed twice by an importer that did not collapse the rows.
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO bom_line (revision_id, part_id, mpn, mpn_norm, manufacturer, "
            "manufacturer_norm, description, value, footprint, package, quantity, "
            "references_json, dnp, match_status, created_at) "
            "SELECT revision_id, part_id, mpn, mpn_norm, manufacturer, manufacturer_norm, "
            "description, value, footprint, package, quantity, '[\"U2\"]', 0, 'matched', "
            "created_at FROM bom_line WHERE revision_id = ?",
            (revision_id,),
        )
        conn.commit()
    finally:
        conn.close()

    build_id = _plan(client, project["id"], revision_id, boards=10).json()["id"]
    refused = client.post(f"/api/builds/{build_id}/commit")

    assert refused.status_code == 409
    assert "short" in refused.json()["detail"]
    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM stock_move").fetchone()["n"] == 1
        assert stock_repo.total_on_hand(conn, int(part["id"])) == 15
        status = conn.execute("SELECT status FROM build WHERE id = ?", (build_id,)).fetchone()
        assert status["status"] == "planning"
    finally:
        conn.close()


def test_commit_refuses_409_when_short_and_writes_nothing(client):
    short_part = client.post(
        "/api/parts", json={"mpn": "RC0402FR-0710KL", "manufacturer": "Yageo"}
    ).json()
    enough_part = client.post("/api/parts", json={"mpn": "NE555", "manufacturer": "TI"}).json()
    _receive(short_part, "Bin A", 5)
    _receive(enough_part, "Bin B", 50)
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(
            ["Reference", "MPN", "Manufacturer"],
            [["R1", "RC0402FR-0710KL", "Yageo"], ["U1", "NE555", "TI"]],
        ),
    )
    revision_id = imported.json()["revision_id"]
    build = _plan(client, project["id"], revision_id, boards=10).json()

    committed = client.post(f"/api/builds/{build['id']}/commit")
    assert committed.status_code == 409
    assert "need 10, have 5" in committed.json()["detail"]
    assert short_part["ipn"] in committed.json()["detail"]

    conn = db.connect()
    try:
        assert stock_repo.total_on_hand(conn, int(enough_part["id"])) == 50
        assert stock_repo.total_on_hand(conn, int(short_part["id"])) == 5
        build_moves = conn.execute(
            "SELECT count(*) AS n FROM stock_move WHERE kind = 'build'"
        ).fetchone()["n"]
        assert build_moves == 0
        status = conn.execute("SELECT status FROM build WHERE id = ?", (build["id"],)).fetchone()[
            "status"
        ]
        assert status == "planning"
    finally:
        conn.close()


def test_commit_prefers_named_location_then_largest(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    _receive(part, "Bin A", 100)
    _receive(part, "Bin B", 3)
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build = _plan(client, project["id"], revision_id, boards=5).json()
    line_id = str(build["lines"][0]["id"])

    committed = client.post(
        f"/api/builds/{build['id']}/commit", json={"locations": {line_id: "Bin B"}}
    )
    assert committed.status_code == 200

    conn = db.connect()
    try:
        items = {i["location"]: i["quantity"] for i in stock_repo.get_stock(conn, int(part["id"]))}
        assert items == {"Bin A": 98, "Bin B": 0}
    finally:
        conn.close()


def test_committed_build_cannot_be_committed_or_abandoned(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    _receive(part, "Bin A", 50)
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=2).json()["id"]

    assert client.post(f"/api/builds/{build_id}/commit").status_code == 200
    assert client.post(f"/api/builds/{build_id}/commit").status_code == 409
    assert client.post(f"/api/builds/{build_id}/abandon").status_code == 409


def test_substitute_coverage_lists_linked_substitute_with_stock_and_covers(client):
    called_for = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    alternative = client.post(
        "/api/parts", json={"mpn": "LM358DR2G", "manufacturer": "onsemi"}
    ).json()
    _receive(alternative, "Bin 7", 25)
    conn = db.connect()
    try:
        subs_repo.add_link(
            conn, int(called_for["id"]), int(alternative["id"]), "equivalent", None, "t"
        )
    finally:
        conn.close()

    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=20).json()["id"]

    build = client.get(f"/api/builds/{build_id}").json()
    line = build["lines"][0]
    assert line["short"] == 20
    assert len(line["substitutes"]) == 1
    assert line["substitutes"][0]["substitute_ipn"] == alternative["ipn"]
    assert line["substitutes"][0]["substitute_on_hand"] == 25
    assert line["substitutes"][0]["covers"] is True


def test_picklist_includes_unmatched_lines_with_empty_ipn(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(
            ["Reference", "MPN", "Manufacturer"],
            [["U1", "LM358N", "TI"], ["U2", "NE555", "TI"]],
        ),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=1).json()["id"]

    response = client.get(f"/api/builds/{build_id}/picklist.csv")
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text)))
    header = rows[0]
    assert header[0] == "ipn"
    assert header[1] == "mpn"
    body = rows[1:]
    by_mpn = {row[1]: row for row in body}
    assert by_mpn["LM358N"][0] == "SK-00001"
    assert by_mpn["NE555"][0] == ""
    assert by_mpn["NE555"][1] == "NE555"


def test_positions_kicad_headers_parse():
    data = _pos_csv(
        ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"],
        [["C7", "10k", "0402", "12.4", "24.4", "180.0", "Top"]],
    )
    placements = parse_positions(data)
    assert len(placements) == 1
    assert placements[0]["reference"] == "C7"
    assert placements[0]["x_mm"] == 12.4
    assert placements[0]["y_mm"] == 24.4
    assert placements[0]["rotation_deg"] == 180.0
    assert placements[0]["side"] == "Top"
    assert placements[0]["value"] == "10k"


def test_positions_alt_headers_mid_and_layer_parse():
    data = _pos_csv(
        ["Reference", "Value", "Footprint", "Mid X", "Mid Y", "Rotation", "Layer"],
        [["R1", "1k", "0603", "1.0", "2.0", "90", "bottom"]],
    )
    placements = parse_positions(data)
    assert placements[0]["reference"] == "R1"
    assert placements[0]["x_mm"] == 1.0
    assert placements[0]["side"] == "Bottom"


def test_positions_whitespace_columns_parse():
    """The default KiCad .pos export pads columns instead of separating them."""
    data = (
        "### Footprint positions - created on 2026-07-08T23:34:18 ###\n"
        "### Printed by KiCad version 10.0.1\n"
        "## Unit = mm, Angle = deg.\n"
        "## Side : All\n"
        "# Ref     Val                     Package                       "
        "PosX       PosY       Rot  Side\n"
        "C202      2.2uF                   C_0402_1005Metric         130.1500   -56.3000"
        "    0.0000  bottom\n"
        "R601      30.1                    R_0402_1005Metric          98.4000   -41.2500"
        "   90.0000  top\n"
    )
    placements = parse_positions(data)

    assert len(placements) == 2
    assert placements[0] == {
        "reference": "C202",
        "side": "Bottom",
        "x_mm": 130.15,
        "y_mm": -56.3,
        "rotation_deg": 0.0,
        "package": "C_0402_1005Metric",
        "value": "2.2uF",
    }
    assert placements[1]["reference"] == "R601"
    assert placements[1]["rotation_deg"] == 90.0
    assert placements[1]["side"] == "Top"


def test_positions_whitespace_row_with_a_space_in_the_value_parses():
    """A value like "10k 1%" shifts every column after it; read from the right."""
    data = (
        "# Ref  Val      Package  PosX  PosY  Rot  Side\n"
        "R1     10k 1%   R_0603   1.0   2.0   0    top\n"
    )
    placements = parse_positions(data)

    assert placements[0]["value"] == "10k 1%"
    assert placements[0]["package"] == "R_0603"
    assert placements[0]["x_mm"] == 1.0
    assert placements[0]["y_mm"] == 2.0


def test_positions_bad_coordinate_names_the_line():
    data = _pos_csv(
        ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"],
        [["R1", "1k", "0402", "foo", "2.0", "0", "Top"]],
    )
    with pytest.raises(PositionError) as exc:
        parse_positions(data)
    assert exc.value.line == 2
    assert "foo" in str(exc.value)


def test_positions_reimport_replaces(client):
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]

    first = _pos_csv(
        ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"],
        [["C7", "10k", "0402", "1.0", "2.0", "0", "Top"]],
    )
    r1 = client.post(
        f"/api/revisions/{revision_id}/positions",
        files={"file": ("a.pos", first, "text/csv")},
    )
    assert r1.status_code == 200
    assert "sha256" in r1.json()

    second = _pos_csv(
        ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"],
        [["R9", "1k", "0603", "3.0", "4.0", "0", "Top"]],
    )
    r2 = client.post(
        f"/api/revisions/{revision_id}/positions",
        files={"file": ("b.pos", second, "text/csv")},
    )
    assert r2.status_code == 200
    assert r2.json()["sha256"] != r1.json()["sha256"]

    conn = db.connect()
    try:
        placements = builds_repo.get_placements(conn, revision_id)
        assert [p["reference"] for p in placements] == ["R9"]
    finally:
        conn.close()


def test_board_xml_placements_dnp_and_part_id(client):
    part = client.post(
        "/api/parts", json={"mpn": "GRM155Z71A225KE44D", "manufacturer": "Murata"}
    ).json()
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev B",
        _csv(
            ["Reference", "MPN", "Manufacturer", "DNP"],
            [
                ["C7", "GRM155Z71A225KE44D", "Murata", ""],
                ["R9", "CRCW040210K0FKED", "Vishay", "DNP"],
            ],
        ),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=1).json()["id"]

    positions = _pos_csv(
        ["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"],
        [
            ["C7", "2.2uF", "0402", "12.4", "24.4", "180.0", "Top"],
            ["R9", "10k", "0402", "1.0", "1.0", "0", "Top"],
            ["U99", "x", "SOIC-8", "2.0", "2.0", "0", "Top"],
        ],
    )
    upload = client.post(
        f"/api/revisions/{revision_id}/positions",
        files={"file": ("board.pos", positions, "text/csv")},
    )
    assert upload.status_code == 200

    response = client.get(f"/api/builds/{build_id}/board.xml")
    assert response.status_code == 200
    root = ET.fromstring(response.text)  # well-formed XML
    placements = root.find("placements")
    assert placements is not None
    children = list(placements)
    assert len(children) == 1  # DNP R9 excluded, U99 unmatched skipped
    placement = children[0]
    assert placement.attrib["id"] == "C7"
    assert placement.attrib["part-id"] == part["ipn"]
    assert placement.attrib["type"] == "Place"
    location = placement.find("location")
    assert location is not None
    assert location.attrib["rotation"] == "180.0"
    assert response.headers["X-Skipped-Placements"] == "1"


def test_board_xml_without_positions_is_409(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=1).json()["id"]

    response = client.get(f"/api/builds/{build_id}/board.xml")
    assert response.status_code == 409
    assert response.json()["detail"] == "positions_required"


def test_viewer_can_download_but_cannot_commit(client, anon_client, make_key):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    _receive(part, "Bin A", 10)
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=1).json()["id"]

    viewer = make_key("viewer")
    anon_client.headers["X-API-Key"] = viewer
    download = anon_client.get(f"/api/builds/{build_id}/picklist.csv")
    assert download.status_code == 200
    write = anon_client.post(f"/api/builds/{build_id}/commit", json={})
    assert write.status_code == 403


def test_a_multi_board_job_names_the_board_once(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    project = _project(client)
    imported = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    revision_id = imported.json()["revision_id"]
    build_id = _plan(client, project["id"], revision_id, boards=4).json()["id"]

    response = client.get(f"/api/builds/{build_id}/job.xml")
    assert response.status_code == 200
    root = ET.fromstring(response.text)
    locations = root.find("board-locations")
    assert locations is not None
    # One location, not four: four copies at 0,0 would be placed on top of each
    # other. The build count lives in Shelfmark; the panel lives in OpenPnP.
    assert len(list(locations)) == 1
    assert list(locations)[0].get("board-file") == "sk-sensor-rev-A.board.xml"
