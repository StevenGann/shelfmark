from app.config import settings


def test_no_key_returns_401(anon_client):
    r = anon_client.get("/api/parts")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_key_returns_401(anon_client):
    r = anon_client.get("/api/parts", headers={"X-API-Key": "smk_" + "0" * 40})
    assert r.status_code == 401


def test_viewer_cannot_create(anon_client, make_key):
    key = make_key("viewer")
    r = anon_client.post(
        "/api/parts",
        json={"mpn": "LM358N", "manufacturer": "TI"},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 403


def test_engineer_can_create(anon_client, make_key):
    key = make_key("engineer")
    r = anon_client.post(
        "/api/parts",
        json={"mpn": "LM358N", "manufacturer": "TI"},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 201


def test_revoked_key_returns_401(anon_client, make_key):
    from app import auth, db

    key = make_key("admin")
    conn = db.connect()
    try:
        row = auth.verify_key(conn, key)
        assert row is not None
        auth.revoke_key(conn, int(row["id"]))
    finally:
        conn.close()

    r = anon_client.get("/api/parts", headers={"X-API-Key": key})
    assert r.status_code == 401


def test_auth_none_allows_without_key(anon_client):
    original = settings.auth
    settings.auth = "none"
    try:
        r = anon_client.get("/api/parts")
        assert r.status_code == 200
    finally:
        settings.auth = original


def test_api_accepts_header_key_and_refuses_session_cookie(anon_client, make_key, make_user):
    key = make_key("viewer")
    assert anon_client.get("/api/parts", headers={"X-API-Key": key}).status_code == 200

    user = make_user("viewer")
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    anon_client.cookies.set("smk_session", r.cookies["smk_session"])

    assert anon_client.get("/api/parts").status_code == 401
