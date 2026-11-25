"""Substitutions: links, shortfall arithmetic and value folding."""

from app import db
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo
from app.repo import substitutes as subs_repo


def _part(mpn="LM358N", manufacturer="TI", *, package=None, value=None):
    conn = db.connect()
    try:
        part = parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
        fields = {}
        if package is not None:
            fields["package"] = package
        if value is not None:
            fields["value"] = value
        if fields:
            part = parts_repo.update_part(conn, part["ipn"], **fields)
        return part
    finally:
        conn.close()


def _link(client, part, substitute, fit="equivalent", note=None):
    return client.post(
        f"/api/parts/{part['ipn']}/substitutes",
        json={"substitute": substitute["ipn"], "fit": fit, "note": note},
    )


def test_a_link_round_trips_and_shows_in_both_directions(client):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")

    created = _link(client, called_for, alternative, "equivalent", "same op-amp")
    assert created.status_code == 201
    assert created.json()["substitute_ipn"] == alternative["ipn"]

    out = client.get(f"/api/parts/{called_for['ipn']}/substitutes").json()["items"]
    assert [row["substitute_ipn"] for row in out] == [alternative["ipn"]]

    into = client.get(f"/api/parts/{alternative['ipn']}/substitutes?direction=in").json()
    assert [row["part_ipn"] for row in into["items"]] == [called_for["ipn"]]


def test_a_part_cannot_substitute_for_itself(client):
    part = _part()
    response = _link(client, part, part)
    assert response.status_code == 422


def test_a_duplicate_link_is_refused(client):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")

    assert _link(client, called_for, alternative).status_code == 201
    assert _link(client, called_for, alternative).status_code == 409


def test_form_fit_needs_a_note(client):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")

    without = _link(client, called_for, alternative, "form-fit")
    assert without.status_code == 422

    with_note = _link(client, called_for, alternative, "form-fit", "0402 body, 0603 land")
    assert with_note.status_code == 201


def test_removing_a_link_clears_both_directions(client):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")
    _link(client, called_for, alternative)

    removed = client.delete(f"/api/parts/{called_for['ipn']}/substitutes/{alternative['ipn']}")
    assert removed.status_code == 200

    assert client.get(f"/api/parts/{called_for['ipn']}/substitutes").json()["items"] == []
    into = client.get(f"/api/parts/{alternative['ipn']}/substitutes?direction=in").json()
    assert into["items"] == []


def test_shortfall_arithmetic_uses_the_ledger(client):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")
    _link(client, called_for, alternative)

    conn = db.connect()
    try:
        stock_repo.record_move(conn, int(called_for["id"]), "Drawer A", "receive", 12, None, "t")
        stock_repo.record_move(conn, int(alternative["id"]), "Bin 7", "receive", 15, None, "t")
        result = subs_repo.shortfall(conn, int(called_for["id"]), 20)
    finally:
        conn.close()

    assert result["on_hand"] == 12
    assert result["short"] == 8
    assert result["substitutes"][0]["substitute_on_hand"] == 15
    assert result["substitutes"][0]["covers"] is True


def test_suggestions_need_the_same_package_and_value(client):
    called_for = _part("C1234", "Murata", package="0402", value="2.2uF")
    same = _part("C5678", "Taiyo Yuden", package="0402", value="2.2 µF")
    _part("C9999", "Murata", package="0603", value="2.2uF")
    _part("C1111", "Murata", package="0402", value="4.7uF")

    suggestions = client.get(f"/api/parts/{called_for['ipn']}/substitute-suggestions").json()
    assert [row["ipn"] for row in suggestions["items"]] == [same["ipn"]]


def test_suggestions_exclude_parts_that_are_already_linked(client):
    called_for = _part("C1234", "Murata", package="0402", value="2.2uF")
    same = _part("C5678", "Taiyo Yuden", package="0402", value="2.2uF")
    _link(client, called_for, same, "equivalent")

    suggestions = client.get(f"/api/parts/{called_for['ipn']}/substitute-suggestions").json()
    assert suggestions["items"] == []


def test_a_viewer_cannot_write_but_can_read(anon_client, make_key):
    called_for = _part("LM358N")
    alternative = _part("LM358DR2G", "onsemi")
    viewer = make_key("viewer")
    anon_client.headers["X-API-Key"] = viewer

    write = anon_client.post(
        f"/api/parts/{called_for['ipn']}/substitutes",
        json={"substitute": alternative["ipn"], "fit": "equivalent"},
    )
    assert write.status_code == 403
    assert anon_client.get(f"/api/parts/{called_for['ipn']}/substitutes").status_code == 200


def test_bom_ingest_fills_a_missing_value_and_never_overwrites_one(client):
    typed = _part("RC0402FR-0710KL", "Yageo", value="10k 1%")
    blank = _part("RC0402JR-0710KL", "Yageo")

    project = client.post("/api/projects", json={"name": "value-fill"}).json()
    csv = (
        b"Reference,Qty,Value,Footprint,MPN,Manufacturer\n"
        b"R1,1,10k,0402,RC0402FR-0710KL,Yageo\n"
        b"R2,1,10k,0402,RC0402JR-0710KL,Yageo\n"
    )
    imported = client.post(
        f"/api/projects/{project['id']}/boms",
        data={"label": "rev A"},
        files={"file": ("BOM.csv", csv, "text/csv")},
    )
    assert imported.status_code == 201

    conn = db.connect()
    try:
        kept = conn.execute(
            "SELECT value, value_norm FROM part WHERE id = ?", (typed["id"],)
        ).fetchone()
        filled = conn.execute(
            "SELECT value, value_norm FROM part WHERE id = ?", (blank["id"],)
        ).fetchone()
    finally:
        conn.close()

    assert kept["value"] == "10k 1%"
    assert filled["value"] == "10k"
    assert filled["value_norm"] == "10k"
