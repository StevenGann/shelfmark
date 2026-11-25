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
