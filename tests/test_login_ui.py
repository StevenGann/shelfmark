def test_unauthenticated_redirects_to_setup_when_no_users(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/setup"


def test_setup_creates_first_admin_then_404(anon_client):
    r = anon_client.get("/setup")
    assert r.status_code == 200

    r = anon_client.post(
        "/setup",
        data={
            "username": "alice",
            "password": "correct-horse-battery-staple",
            "password_confirm": "correct-horse-battery-staple",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    assert anon_client.get("/setup").status_code == 404


def test_unauthenticated_redirects_to_login_with_users(anon_client, make_user):
    make_user("admin")
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=%2F"


def test_login_sets_http_only_cookie_and_renders_ui(anon_client, make_user):
    user = make_user("admin")
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "smk_session" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower()

    anon_client.cookies.set("smk_session", r.cookies["smk_session"])
    listed = anon_client.get("/")
    assert listed.status_code == 200
    assert "No parts yet" in listed.text


def test_wrong_password_and_unknown_username_return_identical_401(anon_client, make_user):
    user = make_user("admin")
    wrong = anon_client.post(
        "/login",
        data={"username": user["username"], "password": "wrong-password-123"},
        follow_redirects=False,
    )
    unknown = anon_client.post(
        "/login",
        data={"username": "nobody", "password": "wrong-password-123"},
        follow_redirects=False,
    )
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert "Invalid username or password" in wrong.text
    assert wrong.text == unknown.text


def test_logout_clears_cookie(anon_client, make_user):
    user = make_user("admin")
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", r.cookies["smk_session"])

    out = anon_client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/login"
    set_cookie = out.headers.get("set-cookie", "").lower()
    assert "smk_session=" in set_cookie
    assert "max-age=0" in set_cookie


def test_disabled_user_session_stops_working(anon_client, make_user):
    from app import db, users

    user = make_user("admin")
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", r.cookies["smk_session"])
    assert anon_client.get("/").status_code == 200

    conn = db.connect()
    try:
        users.set_disabled(conn, user["id"], True)
    finally:
        conn.close()

    assert anon_client.get("/", follow_redirects=False).status_code == 302


def test_password_change_invalidates_other_sessions(anon_client, make_user):
    user = make_user("admin")
    r1 = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    first_token = r1.cookies["smk_session"]
    r2 = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    second_token = r2.cookies["smk_session"]
    assert first_token != second_token

    anon_client.cookies.set("smk_session", second_token)
    resp = anon_client.post(
        "/account",
        data={
            "current_password": user["password"],
            "new_password": "brand-new-password-123",
            "new_password_confirm": "brand-new-password-123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Password changed" in resp.text

    assert anon_client.get("/").status_code == 200

    anon_client.cookies.set("smk_session", first_token)
    assert anon_client.get("/", follow_redirects=False).status_code == 302


def test_successful_login_clears_the_failure_count(anon_client, make_user):
    user = make_user("admin")
    credentials = {"username": user["username"], "password": "wrong-password-123"}

    for _ in range(4):
        assert anon_client.post("/login", data=credentials).status_code == 401

    good = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    assert good.status_code == 303

    # The counter started again at zero, so the next four attempts are rejected
    # as bad passwords rather than as a rate limit.
    for _ in range(4):
        assert anon_client.post("/login", data=credentials).status_code == 401


def test_admin_reset_password_kills_every_session_for_that_user(anon_client, make_user):
    from app import db, users

    admin = make_user("admin")
    target = make_user("viewer")

    login = anon_client.post(
        "/login",
        data={"username": target["username"], "password": target["password"], "next": "/"},
        follow_redirects=False,
    )
    target_token = login.cookies["smk_session"]

    admin_login = anon_client.post(
        "/login",
        data={"username": admin["username"], "password": admin["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", admin_login.cookies["smk_session"])
    reset = anon_client.post(
        f"/users/{target['id']}/password",
        data={"password": "a-different-password-1"},
        follow_redirects=False,
    )
    assert reset.status_code == 303

    anon_client.cookies.set("smk_session", target_token)
    assert anon_client.get("/", follow_redirects=False).status_code == 302

    conn = db.connect()
    try:
        assert users.verify_password(
            users.get_user_by_id(conn, target["id"])["password_hash"], "a-different-password-1"
        )
    finally:
        conn.close()


def test_admin_can_enable_a_disabled_user(anon_client, make_user):
    from app import db, users

    admin = make_user("admin")
    target = make_user("viewer")

    conn = db.connect()
    try:
        users.set_disabled(conn, target["id"], True)
    finally:
        conn.close()

    login = anon_client.post(
        "/login",
        data={"username": admin["username"], "password": admin["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", login.cookies["smk_session"])
    assert (
        anon_client.post(f"/users/{target['id']}/enable", follow_redirects=False).status_code == 303
    )

    again = anon_client.post(
        "/login",
        data={"username": target["username"], "password": target["password"], "next": "/"},
        follow_redirects=False,
    )
    assert again.status_code == 303


def test_next_is_never_protocol_relative(anon_client, make_user):
    user = make_user("admin")
    r = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "//evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
