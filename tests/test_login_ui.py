def test_unauthenticated_redirects_to_login(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=%2F"


def test_login_sets_cookie_and_renders_list(anon_client, make_key):
    key = make_key("admin")
    r = anon_client.post("/login", data={"key": key, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "smk_session" in r.cookies

    anon_client.cookies.set("smk_session", r.cookies["smk_session"])
    listed = anon_client.get("/")
    assert listed.status_code == 200
    assert "No parts yet" in listed.text


def test_logout_clears_cookie(anon_client, make_key):
    key = make_key("admin")
    r = anon_client.post("/login", data={"key": key, "next": "/"}, follow_redirects=False)
    anon_client.cookies.set("smk_session", r.cookies["smk_session"])

    out = anon_client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    set_cookie = out.headers.get("set-cookie", "").lower()
    assert "smk_session=" in set_cookie
    assert "max-age=0" in set_cookie


def test_invalid_key_rerenders_login(anon_client):
    r = anon_client.post(
        "/login", data={"key": "smk_" + "f" * 40, "next": "/"}, follow_redirects=False
    )
    assert r.status_code == 401
    assert "Invalid API key" in r.text
