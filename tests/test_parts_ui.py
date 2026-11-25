def test_create_through_form_and_follow_redirect(user_client):
    r = user_client.post(
        "/parts/new",
        data={"mpn": "LM358N", "manufacturer": "TI", "design_status": "active"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "SK-00001" in r.text

    detail = user_client.get("/parts/SK-00001")
    assert detail.status_code == 200
    assert "LM358N" in detail.text

    listing = user_client.get("/")
    assert listing.status_code == 200
    assert "SK-00001" in listing.text


def test_form_rerenders_with_error_on_duplicate(user_client):
    first = user_client.post(
        "/parts/new",
        data={"mpn": "LM358N", "manufacturer": "TI"},
        follow_redirects=True,
    )
    assert first.status_code == 200

    r = user_client.post(
        "/parts/new",
        data={"mpn": "lm358n", "manufacturer": "TI"},
    )
    assert r.status_code == 200
    assert "already exists" in r.text
    assert "SK-00001" in r.text
    assert 'value="lm358n"' in r.text


def test_form_rerenders_on_validation_error(user_client):
    r = user_client.post("/parts/new", data={"mpn": "", "manufacturer": "TI"})
    assert r.status_code == 200
    assert "MPN is required" in r.text
    assert 'value="TI"' in r.text


def test_failed_datasheet_page_links_to_the_source(user_client):
    """When the download is blocked, the page still offers the supplier link."""
    from app import db
    from app.repo import catalog

    created = user_client.post(
        "/parts/new", data={"mpn": "74279206", "manufacturer": "Wurth"}, follow_redirects=True
    )
    assert created.status_code == 200

    conn = db.connect()
    part_id = conn.execute("select id from part where mpn = '74279206'").fetchone()["id"]
    catalog.add_part_link(
        conn,
        part_id,
        kind="datasheet",
        label="Datasheet",
        url="https://www.we-online.com/components/products/datasheet/74279206.pdf",
        source="digikey",
    )
    catalog.add_part_asset(
        conn,
        part_id,
        kind="datasheet",
        source_url="https://www.we-online.com/components/products/datasheet/74279206.pdf",
        rel_path="parts/SK-00001/datasheet.pdf",
        content_type="",
        bytes=0,
        sha256="",
        fetched_at="2026-11-25T00:00:00Z",
        status="failed",
        error="HTTP 403 for https://www.we-online.com/...",
    )
    conn.close()

    page = user_client.get("/parts/SK-00001")
    assert page.status_code == 200
    assert "Datasheet" in page.text
    assert "we-online.com" in page.text
