import hashlib

from app import assets


def test_asset_resolve_rejects_traversal():
    assert assets.resolve("../secret.txt") is None
    assert assets.resolve("../../secret.txt") is None
    assert assets.resolve("parts/SK-00001/../../../secret.txt") is None
    assert assets.resolve("/etc/passwd") is None
    assert assets.resolve("parts/SK-00001/image.jpg") is not None


def test_served_asset_uses_stored_content_type(user_client):
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="LM358N", manufacturer="TI")
    rel_path = assets.rel_path_for(part["ipn"], "image")
    content = b"\xff\xd8\xff\xe0fakejpeg"
    tmp = assets.tmp_path(rel_path)
    tmp.write_bytes(content)
    assets.commit(tmp, rel_path)
    catalog.add_part_asset(
        conn,
        int(part["id"]),
        kind="image",
        source_url="https://example.com/x.jpg",
        rel_path=rel_path,
        content_type="image/jpeg",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        fetched_at="2024-01-01T00:00:00Z",
        status="ok",
    )
    conn.close()

    r = user_client.get(f"/assets/{rel_path}")
    assert r.status_code == 200
    assert r.content == content
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, max-age=86400"


def test_asset_endpoint_refuses_unknown_rel_path(user_client):
    r = user_client.get("/assets/parts/SK-00001/../../etc/passwd")
    assert r.status_code == 404
