from uuid import uuid4

from app.config import settings


def test_rate_limit_returns_429(client):
    original = settings.rate_limit
    settings.rate_limit = 3
    try:
        statuses = [client.get("/api/parts").status_code for _ in range(3)]
        fourth = client.get("/api/parts")
    finally:
        settings.rate_limit = original

    assert statuses == [200, 200, 200]
    assert fourth.status_code == 429
    assert fourth.headers["Retry-After"]


def test_login_rate_limit_returns_429(anon_client, make_user):
    make_user("admin")  # ensure a user exists so /login does not redirect to /setup
    username = f"rl-{uuid4().hex[:8]}"

    original = settings.rate_limit
    settings.rate_limit = 0  # disable the middleware so only the login limit applies
    try:
        statuses = []
        last = None
        for _ in range(6):
            last = anon_client.post(
                "/login",
                data={"username": username, "password": "wrong-password-123"},
                follow_redirects=False,
            )
            statuses.append(last.status_code)
    finally:
        settings.rate_limit = original

    assert statuses == [401, 401, 401, 401, 401, 429]
    assert last is not None
    assert last.headers["Retry-After"]
