import hashlib

from app import assets


def _create_part():
    from app import db
    from app.repo import parts as repo

    conn = db.connect()
    try:
        return repo.create_part(conn, mpn="LM358N", manufacturer="TI")
    finally:
        conn.close()


def _assets_for(part_id, kind=None):
    from app import db
    from app.repo import catalog

    conn = db.connect()
    try:
        rows = catalog.list_part_assets(conn, int(part_id))
    finally:
        conn.close()
    if kind is not None:
        return [a for a in rows if a["kind"] == kind]
    return rows


def _login_as(anon_client, make_user, role):
    user = make_user(role)
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    anon_client.cookies.set("smk_session", r.cookies["smk_session"])
    return user


def test_pdf_upload_accepted_with_right_bytes_on_disk_and_in_db(user_client):
    part = _create_part()
    pdf = b"%PDF-1.4 fake datasheet"

    r = user_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("datasheet.pdf", pdf, "application/pdf")},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == f"/parts/{part['ipn']}"

    rows = _assets_for(part["id"], "datasheet")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "upload"
    assert row["uploaded_by"] == user_client.user["username"]
    assert row["original_filename"] == "datasheet.pdf"
    assert row["content_type"] == "application/pdf"
    assert row["rel_path"].endswith(".pdf")
    assert row["bytes"] == len(pdf)
    assert assets.absolute(row["rel_path"]).read_bytes() == pdf


def test_html_really_named_pdf_refused(user_client):
    part = _create_part()
    html = b"<html><body>an error page, not a datasheet</body></html>" * 100

    r = user_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("datasheet.pdf", html, "application/pdf")},
    )

    assert r.status_code == 400
    assert "Expected a PDF datasheet" in r.text
    assert not r.text.lstrip().startswith("{")
    assert _assets_for(part["id"], "datasheet") == []


def test_oversize_upload_refused(user_client):
    part = _create_part()
    too_big = b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024)

    r = user_client.post(
        f"/parts/{part['ipn']}/assets/image",
        files={"file": ("photo.jpg", too_big, "image/jpeg")},
    )

    assert r.status_code == 400
    assert "exceeds" in r.text
    assert _assets_for(part["id"], "image") == []


def test_empty_upload_refused(user_client):
    part = _create_part()

    r = user_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("datasheet.pdf", b"", "application/pdf")},
    )

    assert r.status_code == 400
    assert "empty" in r.text
    assert _assets_for(part["id"], "datasheet") == []


def test_viewer_cannot_upload_or_delete(anon_client, make_user):
    part = _create_part()
    _login_as(anon_client, make_user, "viewer")

    up = anon_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("d.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert up.status_code == 403

    delete = anon_client.delete(f"/parts/{part['ipn']}/assets/datasheet")
    assert delete.status_code == 403


def test_upload_replaces_a_fetched_asset(user_client):
    from app import db
    from app.repo import catalog

    part = _create_part()

    fetched = b"\xff\xd8\xff\xe0the-fetched-jpeg"
    rel = assets.rel_path_for(part["ipn"], "image")
    tmp = assets.tmp_path(rel)
    tmp.write_bytes(fetched)
    assets.commit(tmp, rel)
    conn = db.connect()
    try:
        catalog.add_part_asset(
            conn,
            int(part["id"]),
            kind="image",
            source_url="https://example.com/i.jpg",
            rel_path=rel,
            content_type="image/jpeg",
            bytes=len(fetched),
            sha256=hashlib.sha256(fetched).hexdigest(),
            fetched_at="2026-11-25T00:00:00Z",
            status="ok",
        )
    finally:
        conn.close()

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    r = user_client.post(
        f"/parts/{part['ipn']}/assets/image",
        files={"file": ("photo.png", png, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    rows = _assets_for(part["id"], "image")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "upload"
    assert row["rel_path"].endswith(".png")
    assert assets.absolute(row["rel_path"]).read_bytes() == png
    assert not assets.absolute(rel).exists()


def test_enrichment_skips_an_uploaded_asset(user_client, monkeypatch):
    from app import db, enrich
    from app.repo import catalog

    part = _create_part()

    uploaded = b"%PDF-1.4 uploaded by hand"
    r = user_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("datasheet.pdf", uploaded, "application/pdf")},
        follow_redirects=False,
    )
    assert r.status_code == 303

    fetched = b"%PDF-1.4 fetched from digikey"
    called: list[str] = []

    def fake_download(url, dest, *, max_bytes, allowed_types, timeout=30):
        called.append(url)
        with open(dest, "wb") as handle:
            handle.write(fetched)
        return len(fetched), hashlib.sha256(fetched).hexdigest(), "application/pdf"

    data = {
        "Product": {
            "Manufacturer": {"Name": "TI"},
            "DatasheetUrl": "https://example.com/d.pdf",
            "PhotoUrl": None,
        }
    }
    monkeypatch.setattr(enrich, "fetch_product_details", lambda mpn: data)
    monkeypatch.setattr(enrich, "download", fake_download)

    conn = db.connect()
    try:
        catalog.queue_job(conn, int(part["id"]))
        enrich.run_queue(conn)
    finally:
        conn.close()

    rows = _assets_for(part["id"], "datasheet")
    assert len(rows) == 1
    assert rows[0]["source"] == "upload"
    assert assets.absolute(rows[0]["rel_path"]).read_bytes() == uploaded
    assert called == []


def test_delete_removes_row_and_file(user_client):
    part = _create_part()
    pdf = b"%PDF-1.4 to be deleted"

    up = user_client.post(
        f"/parts/{part['ipn']}/assets/datasheet",
        files={"file": ("datasheet.pdf", pdf, "application/pdf")},
        follow_redirects=False,
    )
    assert up.status_code == 303
    rel_path = _assets_for(part["id"], "datasheet")[0]["rel_path"]
    assert assets.absolute(rel_path).exists()

    r = user_client.delete(f"/parts/{part['ipn']}/assets/datasheet", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/parts/{part['ipn']}"
    assert _assets_for(part["id"], "datasheet") == []
    assert not assets.absolute(rel_path).exists()
