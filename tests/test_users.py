def _user_by_id(user_id):
    from app import db, users

    conn = db.connect()
    try:
        return users.get_user_by_id(conn, user_id)
    finally:
        conn.close()


def test_admin_cannot_demote_or_disable_themselves(user_client):
    actor = user_client.user
    r = user_client.post(
        f"/users/{actor['id']}/role", data={"role": "engineer"}, follow_redirects=False
    )
    assert r.status_code == 400
    assert "cannot change your own role" in r.text

    r = user_client.post(f"/users/{actor['id']}/disable", follow_redirects=False)
    assert r.status_code == 400
    assert "cannot disable yourself" in r.text

    still = _user_by_id(actor["id"])
    assert still["role"] == "admin"
    assert still["disabled_at"] is None


def test_admin_can_create_and_manage_other_users(user_client, make_user):
    r = user_client.post(
        "/users",
        data={"username": "bob", "password": "correct-horse-battery-staple", "role": "viewer"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/users"

    second = make_user("admin")
    r = user_client.post(
        f"/users/{second['id']}/role", data={"role": "engineer"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert _user_by_id(second["id"])["role"] == "engineer"

    r = user_client.post(f"/users/{second['id']}/disable", follow_redirects=False)
    assert r.status_code == 303
    assert _user_by_id(second["id"])["disabled_at"] is not None


def test_disabling_a_user_ends_their_sessions(anon_client, make_user):
    admin = make_user("admin")
    victim = make_user("viewer")

    r = anon_client.post(
        "/login",
        data={"username": victim["username"], "password": victim["password"], "next": "/"},
        follow_redirects=False,
    )
    victim_token = r.cookies["smk_session"]
    anon_client.cookies.set("smk_session", victim_token)
    assert anon_client.get("/", follow_redirects=False).status_code == 200

    admin_login = anon_client.post(
        "/login",
        data={"username": admin["username"], "password": admin["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", admin_login.cookies["smk_session"])

    r = anon_client.post(f"/users/{victim['id']}/disable", follow_redirects=False)
    assert r.status_code == 303

    anon_client.cookies.set("smk_session", victim_token)
    assert anon_client.get("/", follow_redirects=False).status_code == 302


def test_duplicate_username_is_rejected(user_client):
    r = user_client.post(
        "/users",
        data={"username": "dup", "password": "correct-horse-battery-staple", "role": "viewer"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = user_client.post(
        "/users",
        data={"username": "dup", "password": "correct-horse-battery-staple", "role": "viewer"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "already taken" in r.text


def test_short_password_is_rejected_on_setup(anon_client):
    r = anon_client.post(
        "/setup",
        data={"username": "alice", "password": "short", "password_confirm": "short"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "at least 12 characters" in r.text
