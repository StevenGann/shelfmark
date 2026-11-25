"""Every page a person can reach, driven through a session like a browser.

These complement the API tests: the same features are exercised through the HTML routes, because
that is where a session-cookie bug hides while ``/api`` keeps passing. Each test logs in as a real
user and follows the redirects a browser would follow.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.repo import boms as boms_repo
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo

BOM = b"Reference,Qty,Value,Footprint,MPN,Manufacturer\nR1,1,10k,0402,RC0402FR-0710KL,Yageo\n"


def _login(make_user, role: str = "admin") -> TestClient:
    user = make_user(role)
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    client.cookies.set("smk_session", response.cookies["smk_session"])
    client.user = user
    return client


@pytest.fixture
def browser(make_user):
    """Factory: a logged-in browser session for a role, closed at the end of the test."""
    opened: list[TestClient] = []

    def _make(role: str = "admin") -> TestClient:
        client = _login(make_user, role)
        opened.append(client)
        return client

    yield _make

    for client in opened:
        client.__exit__(None, None, None)


def _part(mpn: str = "RC0402FR-0710KL", manufacturer: str = "Yageo") -> dict:
    conn = db.connect()
    try:
        return parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
    finally:
        conn.close()


def _receive(part: dict, location: str, quantity: int) -> None:
    conn = db.connect()
    try:
        stock_repo.record_move(conn, int(part["id"]), location, "receive", quantity, None, "t")
    finally:
        conn.close()


def test_first_run_goes_to_setup_and_creates_the_admin(anon_client):
    assert anon_client.get("/", follow_redirects=False).headers["location"] == "/setup"

    page = anon_client.get("/setup")
    assert page.status_code == 200

    created = anon_client.post(
        "/setup",
        data={
            "username": "first",
            "password": "a-long-enough-passphrase",
            "password_confirm": "a-long-enough-passphrase",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    # The setup page is gone once an account exists.
    assert anon_client.get("/setup").status_code == 404
    assert anon_client.get("/login").status_code == 200


def test_setup_rejects_a_mismatched_confirmation(anon_client):
    response = anon_client.post(
        "/setup",
        data={"username": "first", "password": "one-passphrase", "password_confirm": "another"},
    )
    assert response.status_code == 400
    assert "match" in response.text.lower()


def test_login_rejects_a_bad_password_and_accepts_a_good_one(make_user):
    user = make_user("admin")
    client = TestClient(app)
    with client:
        bad = client.post(
            "/login",
            data={"username": user["username"], "password": "wrong", "next": "/"},
            follow_redirects=False,
        )
        assert bad.status_code == 401

        good = client.post(
            "/login",
            data={"username": user["username"], "password": user["password"], "next": "/"},
            follow_redirects=False,
        )
        assert good.status_code == 303


def test_logout_ends_the_session(browser):
    client = browser("admin")
    assert client.get("/").status_code == 200

    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303

    # The cookie is cleared, so the dashboard cannot be read any more.
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code == 302


def test_the_dashboard_lists_parts(browser):
    part = _part()
    page = browser("viewer").get("/")
    assert page.status_code == 200
    assert part["ipn"] in page.text


def test_a_part_is_created_and_edited_through_the_forms(browser):
    client = browser("engineer")

    created = client.post(
        "/parts/new",
        data={
            "mpn": "LM358N",
            "manufacturer": "TI",
            "description": "Dual op-amp",
            "category": "analog",
            "design_status": "active",
            "notes": "bench stock",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    ipn = created.headers["location"].rsplit("/", 1)[-1]
    assert ipn.startswith("SK-")

    edited = client.post(
        f"/parts/{ipn}",
        data={
            "mpn": "LM358N",
            "manufacturer": "Texas Instruments",
            "description": "Dual op-amp, SOIC-8",
            "design_status": "active",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303

    page = client.get(f"/parts/{ipn}")
    assert page.status_code == 200
    assert "Texas Instruments" in page.text


def test_creating_a_part_without_an_mpn_explains_itself(browser):
    page = browser("engineer").post("/parts/new", data={"mpn": "", "manufacturer": "TI"})
    assert page.status_code == 200
    assert "MPN is required" in page.text


def test_a_duplicate_part_is_refused_on_the_form(browser):
    _part()
    page = browser("engineer").post(
        "/parts/new", data={"mpn": "RC0402FR-0710KL", "manufacturer": "Yageo"}
    )
    assert page.status_code == 200
    assert "already exists" in page.text


def test_stock_is_received_moved_and_refused_when_it_would_go_negative(browser):
    part = _part()
    client = browser("engineer")

    received = client.post(
        f"/parts/{part['ipn']}/stock",
        data={"kind": "receive", "location": "Bin 7", "quantity": "50", "note": "delivery"},
        follow_redirects=False,
    )
    assert received.status_code == 303

    moved = client.post(
        f"/parts/{part['ipn']}/stock/move",
        data={"from_location": "Bin 7", "to_location": "Bin 9", "quantity": "20"},
        follow_redirects=False,
    )
    assert moved.status_code == 303

    conn = db.connect()
    try:
        by_location = {
            i["location"]: i["quantity"] for i in stock_repo.get_stock(conn, int(part["id"]))
        }
    finally:
        conn.close()
    assert by_location == {"Bin 7": 30, "Bin 9": 20}

    refused = client.post(
        f"/parts/{part['ipn']}/stock",
        data={"kind": "consume", "location": "Bin 7", "quantity": "999"},
    )
    assert refused.status_code == 409
    assert "only 30 in stock" in refused.text

    bad_number = client.post(
        f"/parts/{part['ipn']}/stock",
        data={"kind": "receive", "location": "Bin 7", "quantity": "lots"},
    )
    assert bad_number.status_code == 400


def test_offers_are_added_listed_and_deleted(browser):
    part = _part()
    client = browser("engineer")

    added = client.post(
        f"/parts/{part['ipn']}/offers",
        data={
            "distributor": "DigiKey",
            "sku": "311-10.0KLRCT-ND",
            "minimum_quantity": "1",
            "break_quantity": "1",
            "break_price": "0.0034",
            "packaging": "Cut Tape",
            "url": "https://www.digikey.com/en/products/result?keywords=311-10.0KLRCT-ND",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303

    page = client.get(f"/parts/{part['ipn']}")
    assert "311-10.0KLRCT-ND" in page.text

    conn = db.connect()
    try:
        offer_id = conn.execute("SELECT id FROM offer ORDER BY id DESC LIMIT 1").fetchone()["id"]
    finally:
        conn.close()

    removed = client.post(f"/offers/{offer_id}/delete", follow_redirects=False)
    assert removed.status_code == 303
    assert "311-10.0KLRCT-ND" not in client.get(f"/parts/{part['ipn']}").text


def test_refreshing_offers_without_credentials_says_so(browser):
    part = _part()
    page = browser("engineer").post(f"/parts/{part['ipn']}/offers/refresh")
    assert page.status_code == 502
    assert "credential" in page.text.lower()


def test_enrichment_can_be_queued_from_the_part_page(browser):
    part = _part()
    client = browser("engineer")
    assert client.post(f"/parts/{part['ipn']}/enrich", follow_redirects=False).status_code == 303

    conn = db.connect()
    try:
        queued = conn.execute("SELECT count(*) AS n FROM fetch_job").fetchone()["n"]
    finally:
        conn.close()
    assert queued == 1


def test_substitutes_are_added_and_removed_and_form_fit_needs_a_note(browser):
    part = _part("LM358N", "TI")
    alternative = _part("LM358DR2G", "onsemi")
    client = browser("engineer")

    added = client.post(
        f"/parts/{part['ipn']}/substitutes",
        data={"substitute": alternative["ipn"], "fit": "equivalent", "note": "same die"},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert alternative["ipn"] in client.get(f"/parts/{part['ipn']}").text

    without_note = client.post(
        f"/parts/{part['ipn']}/substitutes",
        data={"substitute": alternative["ipn"], "fit": "form-fit", "note": ""},
    )
    assert without_note.status_code == 400
    assert "note" in without_note.text.lower()

    removed = client.post(
        f"/parts/{part['ipn']}/substitutes/delete",
        data={"part_ipn": part["ipn"], "substitute_ipn": alternative["ipn"]},
        follow_redirects=False,
    )
    assert removed.status_code == 303


def test_a_viewer_cannot_change_anything(browser):
    part = _part()
    client = browser("viewer")

    assert client.get(f"/parts/{part['ipn']}").status_code == 200
    refused = client.post(
        f"/parts/{part['ipn']}/stock",
        data={"kind": "receive", "location": "Bin 1", "quantity": "1"},
    )
    assert refused.status_code == 403


def test_the_stock_page_filters_by_location(browser):
    part = _part()
    _receive(part, "Bin 4", 10)
    client = browser("viewer")

    page = client.get("/stock", params={"location": "Bin 4"})
    assert page.status_code == 200
    assert part["ipn"] in page.text

    empty = client.get("/stock", params={"location": "Nowhere"})
    assert part["ipn"] not in empty.text


def test_scanning_an_empty_upload_asks_for_a_photo(browser):
    page = browser("engineer").post("/stock/scan", data={})
    assert page.status_code == 400
    assert "Choose a label photo" in page.text


def test_scanning_an_image_without_a_barcode_says_so(browser):
    import io

    from PIL import Image

    blank = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(blank, format="PNG")

    page = browser("engineer").post(
        "/stock/scan", files={"file": ("label.png", blank.getvalue(), "image/png")}
    )
    assert page.status_code == 422
    assert "No barcode" in page.text


def test_a_project_is_created_and_a_bom_is_imported_in_two_steps(browser):
    part = _part()
    client = browser("engineer")

    created = client.post("/projects", data={"name": "widget", "description": "demo"})
    assert created.status_code in (200, 303)
    conn = db.connect()
    try:
        project = boms_repo.get_project_by_name(conn, "widget")
    finally:
        conn.close()
    assert project is not None

    preview = client.post(
        f"/projects/{project['id']}/boms",
        data={"label": "rev A"},
        files={"file": ("BOM.csv", BOM, "text/csv")},
    )
    assert preview.status_code == 200
    assert "rev A" in preview.text

    # The confirmation carries the sha256 of the staged file back.
    import hashlib

    confirmed = client.post(
        f"/projects/{project['id']}/boms",
        data={
            "_confirm": "1",
            "label": "rev A",
            "filename": "BOM.csv",
            "sha256": hashlib.sha256(BOM).hexdigest(),
        },
        follow_redirects=False,
    )
    assert confirmed.status_code == 303

    conn = db.connect()
    try:
        revisions = boms_repo.list_revisions(conn, int(project["id"]))
        assert len(revisions) == 1
        revision_id = int(revisions[0]["id"])
        line = conn.execute(
            "SELECT part_id FROM bom_line WHERE revision_id = ?", (revision_id,)
        ).fetchone()
    finally:
        conn.close()
    assert int(line["part_id"]) == int(part["id"])


def test_an_unmatched_line_can_be_resolved_by_ipn(browser):
    part = _part("NE555", "TI")
    client = browser("engineer")

    client.post("/projects", data={"name": "widget"})
    conn = db.connect()
    try:
        project = boms_repo.get_project_by_name(conn, "widget")
        imported = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=b"Reference,Qty,MPN,Manufacturer\nU1,1,NE555,TI\n",
            imported_by="t",
        )
        revision_id = int(imported["revision_id"])
        line_id = int(
            conn.execute(
                "SELECT id FROM bom_line WHERE revision_id = ?", (revision_id,)
            ).fetchone()["id"]
        )
    finally:
        conn.close()

    resolved = client.post(
        f"/bom-lines/{line_id}/resolve",
        data={"ipn": part["ipn"], "return_to": f"/revisions/{revision_id}"},
        follow_redirects=False,
    )
    assert resolved.status_code == 303

    conn = db.connect()
    try:
        row = conn.execute("SELECT part_id FROM bom_line WHERE id = ?", (line_id,)).fetchone()
        assert int(row["part_id"]) == int(part["id"])
    finally:
        conn.close()


def test_a_build_is_planned_and_committed_from_the_pages(browser):
    part = _part()
    _receive(part, "Bin 2", 20)
    client = browser("engineer")

    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="widget")
        imported = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(imported["revision_id"])
    finally:
        conn.close()

    planned = client.post(
        f"/revisions/{revision_id}/builds", data={"boards": "10"}, follow_redirects=False
    )
    assert planned.status_code == 303
    build_id = int(planned.headers["location"].rsplit("/", 1)[-1])

    page = client.get(f"/builds/{build_id}")
    assert page.status_code == 200
    assert part["ipn"] in page.text

    committed = client.post(f"/builds/{build_id}/commit", follow_redirects=False)
    assert committed.status_code == 303

    conn = db.connect()
    try:
        assert stock_repo.total_on_hand(conn, int(part["id"])) == 10
    finally:
        conn.close()


def test_a_short_build_is_refused_on_the_page_with_the_numbers(browser):
    part = _part()
    _receive(part, "Bin 2", 3)
    client = browser("engineer")

    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="widget")
        imported = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(imported["revision_id"])
    finally:
        conn.close()

    planned = client.post(
        f"/revisions/{revision_id}/builds", data={"boards": "10"}, follow_redirects=False
    )
    build_id = int(planned.headers["location"].rsplit("/", 1)[-1])

    refused = client.post(f"/builds/{build_id}/commit")
    assert refused.status_code == 409
    assert "short" in refused.text.lower()

    conn = db.connect()
    try:
        assert stock_repo.total_on_hand(conn, int(part["id"])) == 3
    finally:
        conn.close()


def test_planning_a_build_needs_a_positive_board_count(browser):
    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="widget")
        imported = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(imported["revision_id"])
    finally:
        conn.close()

    page = browser("engineer").post(f"/revisions/{revision_id}/builds", data={"boards": "0"})
    assert page.status_code == 400
    assert "whole number" in page.text


def test_the_build_downloads_are_served_from_the_page_routes(browser):
    part = _part()
    client = browser("viewer")

    conn = db.connect()
    try:
        project = boms_repo.create_project(conn, name="widget")
        imported = boms_repo.import_bom(
            conn,
            project_id=int(project["id"]),
            label="rev A",
            source_filename="BOM.csv",
            data=BOM,
            imported_by="t",
        )
        revision_id = int(imported["revision_id"])
        build = builds_create(conn, revision_id)
    finally:
        conn.close()

    picklist = client.get(f"/builds/{build}/picklist.csv")
    assert picklist.status_code == 200
    assert part["ipn"] in picklist.text

    # No positions yet: the board file says what is missing instead of inventing coordinates.
    board = client.get(f"/builds/{build}/board.xml")
    assert board.status_code == 409
    assert "positions" in board.text.lower()

    job = client.get(f"/builds/{build}/job.xml")
    assert job.status_code == 200
    assert "<openpnp-job>" in job.text


def builds_create(conn, revision_id: int) -> int:
    from app.repo import builds as builds_repo

    return int(
        builds_repo.create_build(
            conn, revision_id=revision_id, boards=1, note=None, created_by="t"
        )["id"]
    )


def test_users_can_be_created_promoted_disabled_and_repassworded(browser):
    client = browser("admin")

    created = client.post(
        "/users",
        data={"username": "ada", "password": "a-long-enough-passphrase", "role": "viewer"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    conn = db.connect()
    try:
        user_id = int(
            conn.execute("SELECT id FROM app_user WHERE username = 'ada'").fetchone()["id"]
        )
    finally:
        conn.close()

    assert (
        client.post(
            f"/users/{user_id}/role", data={"role": "engineer"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert client.post(f"/users/{user_id}/disable", follow_redirects=False).status_code == 303
    assert client.post(f"/users/{user_id}/enable", follow_redirects=False).status_code == 303
    assert (
        client.post(
            f"/users/{user_id}/password",
            data={"password": "another-long-passphrase"},
            follow_redirects=False,
        ).status_code
        == 303
    )

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT role, disabled_at FROM app_user WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["role"] == "engineer"
    assert row["disabled_at"] is None


def test_a_person_can_change_their_own_password(browser):
    client = browser("engineer")
    page = client.post(
        "/account",
        data={
            "current_password": client.user["password"],
            "new_password": "an-entirely-new-passphrase",
            "new_password_confirm": "an-entirely-new-passphrase",
        },
    )
    assert page.status_code in (200, 303)

    fresh = TestClient(app)
    with fresh:
        ok = fresh.post(
            "/login",
            data={
                "username": client.user["username"],
                "password": "an-entirely-new-passphrase",
                "next": "/",
            },
            follow_redirects=False,
        )
        assert ok.status_code == 303


def test_pages_that_do_not_exist_are_404_and_unknown_ipns_too(browser):
    client = browser("admin")
    assert client.get("/parts/SK-99999").status_code == 404
    assert client.get("/projects/424242").status_code == 404
    assert client.get("/revisions/424242").status_code == 404
    assert client.get("/builds/424242").status_code == 404
