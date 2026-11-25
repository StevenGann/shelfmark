import hashlib

import pytest

from app import assets, db
from app.bom import NoMpnColumnError, expand_references, parse_bom
from app.repo import boms as boms_repo


def _csv(headers, rows, delimiter=","):
    out = [delimiter.join(headers)]
    for row in rows:
        out.append(delimiter.join(str(c) for c in row))
    return "\n".join(out).encode()


def _import(client, project_id, label, data, **params):
    return client.post(
        f"/api/projects/{project_id}/boms",
        params=params,
        data={"label": label},
        files={"file": ("BOM.csv", data, "text/csv")},
    )


def _project(client, name="sk-sensor"):
    return client.post("/api/projects", json={"name": name}).json()


def test_header_aliases_across_two_kicad_versions():
    data_a = _csv(
        ["Reference", "Qty", "Value", "Footprint", "MPN", "Manufacturer"],
        [["R1", "1", "10k", "0402", "RC0402FR-0710KL", "Yageo"]],
    )
    data_b = _csv(
        ["References", "Quantity", "Comment", "Package", "Mfr Part #", "Mfr"],
        [["R1", "1", "10k", "0402", "RC0402FR-0710KL", "Yageo"]],
    )
    a = parse_bom(data_a)
    b = parse_bom(data_b)
    assert len(a) == len(b) == 1
    assert a[0].mpn == b[0].mpn == "RC0402FR-0710KL"
    assert a[0].mpn_norm == b[0].mpn_norm
    assert a[0].manufacturer == b[0].manufacturer == "Yageo"
    assert a[0].package == b[0].package == "0402"
    assert a[0].references == b[0].references == ["R1"]


def test_delimiter_sniffing_comma_semicolon_tab():
    headers = ["Reference", "MPN", "Manufacturer"]
    rows = [["R1", "LM358N", "TI"]]
    for delimiter in (",", ";", "\t"):
        parsed = parse_bom(_csv(headers, rows, delimiter))
        assert parsed[0].mpn == "LM358N"
        assert parsed[0].references == ["R1"]


def test_reference_range_expansion_and_natural_sort():
    assert expand_references("C1-C3") == ["C1", "C2", "C3"]
    assert expand_references("R10, R2, R1") == ["R1", "R2", "R10"]
    assert expand_references("C1 C3 C2; R1-R2") == ["C1", "C2", "C3", "R1", "R2"]


def test_collapse_duplicate_rows():
    data = _csv(
        ["Reference", "MPN", "Manufacturer"],
        [["R1", "LM358N", "TI"], ["R2", "lm358n", "TI"], ["R3", "LM358N", "TI"]],
    )
    lines = parse_bom(data)
    assert len(lines) == 1
    assert lines[0].references == ["R1", "R2", "R3"]
    assert lines[0].quantity == 3


def test_dnp_kept():
    marked = parse_bom(
        _csv(["Reference", "MPN", "Manufacturer", "DNP"], [["R1", "LM358N", "TI", "DNP"]])
    )
    assert marked[0].dnp is True
    plain = parse_bom(
        _csv(["Reference", "MPN", "Manufacturer", "DNP"], [["R1", "LM358N", "TI", ""]])
    )
    assert plain[0].dnp is False


def test_no_mpn_column_refused_with_seen_headers():
    data = _csv(["Reference", "Qty", "Value"], [["R1", "1", "10k"]])
    with pytest.raises(NoMpnColumnError) as exc:
        parse_bom(data)
    assert exc.value.headers == ["Reference", "Qty", "Value"]


def test_matching_by_mpn_and_manufacturer(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    project = _project(client)
    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]]),
    )
    assert r.status_code == 201
    assert r.json()["unmatched"] == []
    lines = client.get(f"/api/revisions/{r.json()['revision_id']}").json()["lines"]
    assert lines[0]["match_status"] == "matched"
    assert lines[0]["ipn"] == "SK-00001"


def test_ambiguous_match_when_only_mpn_matches_two_parts(client):
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"})
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "On Semi"})
    project = _project(client)
    r = _import(client, project["id"], "rev A", _csv(["Reference", "MPN"], [["U1", "LM358N"]]))
    assert r.status_code == 201
    lines = client.get(f"/api/revisions/{r.json()['revision_id']}").json()["lines"]
    assert lines[0]["match_status"] == "ambiguous"


def test_footprint_overrides_part_package(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    assert part["package"] is None
    project = _project(client)
    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer", "Footprint"], [["U1", "LM358N", "TI", "8-SOIC"]]),
    )
    assert r.status_code == 201
    got = client.get(f"/api/parts/{part['ipn']}")
    assert got.json()["package"] == "SOIC-8"


def test_diff_add_remove_change_unchanged(client):
    project = _project(client)
    first = _import(
        client,
        project["id"],
        "rev A",
        _csv(
            ["Reference", "MPN", "Manufacturer"],
            [["R1", "A", "M"], ["R2", "B", "M"], ["R3", "C", "M"]],
        ),
    ).json()
    assert first["previous"] is None
    assert [a["mpn"] for a in first["added"]] == ["A", "B", "C"]

    second = _import(
        client,
        project["id"],
        "rev B",
        _csv(
            ["Reference", "MPN", "Manufacturer"],
            [["R1", "A", "M"], ["R3 R5", "C", "M"], ["R4", "D", "M"]],
        ),
    ).json()
    assert second["unchanged_count"] == 1
    assert [r["mpn"] for r in second["removed"]] == ["B"]
    assert [a["mpn"] for a in second["added"]] == ["D"]
    assert len(second["changed"]) == 1
    changed = second["changed"][0]
    assert changed["from"]["quantity"] == 1
    assert changed["to"]["quantity"] == 2
    assert changed["references_added"] == ["R5"]
    assert changed["references_removed"] == []


def test_dry_run_writes_nothing(client):
    project = _project(client)
    data = _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]])
    r = _import(client, project["id"], "rev A", data, dry_run="1")
    assert r.status_code == 201
    body = r.json()
    assert body["dry_run"] is True
    assert body["revision_id"] is None

    conn = db.connect()
    try:
        assert conn.execute("SELECT count(*) AS n FROM bom_revision").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM bom_line").fetchone()["n"] == 0
    finally:
        conn.close()

    sha256 = hashlib.sha256(data).hexdigest()
    assert not assets.absolute(boms_repo.bom_rel_path("sk-sensor", sha256)).exists()


def test_identical_reimport_is_409(client):
    project = _project(client)
    data = _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]])
    first = _import(client, project["id"], "rev A", data)
    assert first.status_code == 201
    revision_id = first.json()["revision_id"]

    second = _import(client, project["id"], "rev B", data)
    assert second.status_code == 409
    assert second.json()["detail"]["revision_id"] == revision_id


def test_resolve_attaches_line_to_existing_ipn(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    project = _project(client)
    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "NE555", "TI"]]),
    )
    revision_id = r.json()["revision_id"]
    line = client.get(f"/api/revisions/{revision_id}").json()["lines"][0]
    assert line["match_status"] == "unmatched"

    res = client.post(f"/api/bom-lines/{line['id']}/resolve", json={"ipn": part["ipn"]})
    assert res.status_code == 200
    assert res.json()["ipn"] == part["ipn"]
    assert res.json()["match_status"] == "matched"


def test_resolve_unmatched_creates_parts_and_links(client):
    project = _project(client)
    data = _csv(
        ["Reference", "MPN", "Manufacturer"],
        [["U1", "NE555", "TI"], ["U2", "LM358N", "TI"]],
    )
    r = _import(client, project["id"], "rev A", data)
    revision_id = r.json()["revision_id"]

    res = client.post(f"/api/revisions/{revision_id}/resolve-unmatched")
    assert res.status_code == 200
    assert len(res.json()["created"]) == 2

    lines = client.get(f"/api/revisions/{revision_id}").json()["lines"]
    assert all(line["match_status"] == "matched" for line in lines)
    assert {line["ipn"] for line in lines} == {"SK-00001", "SK-00002"}


def test_viewer_gets_403_on_writes(anon_client, make_key):
    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="p")
    finally:
        conn.close()
    key = make_key("viewer")
    data = _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "TI"]])
    r = anon_client.post(
        f"/api/projects/{project['id']}/boms",
        headers={"X-API-Key": key},
        data={"label": "rev A"},
        files={"file": ("BOM.csv", data, "text/csv")},
    )
    assert r.status_code == 403


def test_the_real_sk_sensor_header_set_parses():
    """The export that actually exists: five columns, Customer Reference."""
    csv_text = (
        "Quantity,Manufacturer Part Number,Manufacturer,Customer Reference,Description\n"
        '3,GRM155Z71A225KE44D,Murata Electronics,"C201,C202,C206",2.2uF | MLCC 10V 0402 X7R\n'
        '3,ADS127L14IRSHT,Texas Instruments,"U501,U502,U503",ADC 24-bit\n'
    )
    from app import bom

    lines = bom.parse_bom(csv_text)

    assert [line.mpn for line in lines] == ["GRM155Z71A225KE44D", "ADS127L14IRSHT"]
    assert lines[0].quantity == 3
    assert len(lines[0].references) == 3
    assert lines[0].references == ["C201", "C202", "C206"]
    assert lines[1].references == ["U501", "U502", "U503"]


def test_manufacturer_spelling_with_accents_still_matches(client):
    """A BOM that writes "Wurth" must still find the part stored as "Würth"."""
    client.post("/api/parts", json={"mpn": "742792651", "manufacturer": "Würth Elektronik"})
    project = _project(client)
    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(
            ["Reference", "MPN", "Manufacturer"],
            [["FB201", "742792651", "Wurth Elektronik"]],
        ),
    )
    assert r.status_code == 201
    lines = client.get(f"/api/revisions/{r.json()['revision_id']}").json()["lines"]
    assert lines[0]["match_status"] == "matched"
    assert lines[0]["ipn"] == "SK-00001"


def test_mpn_under_another_manufacturer_says_so(client):
    """An unmatched line must explain which part it nearly matched."""
    client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "On Semi"})
    project = _project(client)
    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["U1", "LM358N", "Texas Instruments"]]),
    )
    assert r.status_code == 201
    lines = client.get(f"/api/revisions/{r.json()['revision_id']}").json()["lines"]
    assert lines[0]["match_status"] == "unmatched"
    assert "different manufacturer" in lines[0]["match_note"]
    assert "SK-00001" in lines[0]["match_note"]


def test_exact_manufacturer_spelling_beats_the_folded_duplicate(client):
    """Two catalogue rows that fold to one name must not make the line ambiguous."""
    first = client.post(
        "/api/parts", json={"mpn": "74405042068", "manufacturer": "Würth Elektronik"}
    ).json()
    client.post("/api/parts", json={"mpn": "74405042068", "manufacturer": "Wurth Elektronik"})
    project = _project(client)

    r = _import(
        client,
        project["id"],
        "rev A",
        _csv(["Reference", "MPN", "Manufacturer"], [["L402", "74405042068", "Wurth Elektronik"]]),
    )
    assert r.status_code == 201
    lines = client.get(f"/api/revisions/{r.json()['revision_id']}").json()["lines"]
    assert lines[0]["match_status"] == "matched"
    assert lines[0]["ipn"] != first["ipn"]
