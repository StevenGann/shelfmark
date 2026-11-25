import os
import tempfile
from uuid import uuid4

import pytest

# Configure a scratch data directory before any test imports the app: the
# settings object is built at import time.
os.environ.setdefault("SHELFMARK_DATA_DIR", tempfile.mkdtemp(prefix="shelfmark-test-"))


@pytest.fixture(autouse=True)
def reset_db():
    """Reset the parts tables before each test so tests are independent.

    The data directory is shared across the suite, so parts and the IPN counter
    left behind by one test would leak into the next.
    """
    from app import db

    db.migrate()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM part")
        conn.execute("UPDATE ipn_counter SET last_value = 0 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    yield


@pytest.fixture()
def make_key():
    """Create an API key of a given role and return its plaintext."""

    from app import auth, db

    def _make(role: str, name: str | None = None) -> str:
        db.migrate()
        conn = db.connect()
        try:
            return auth.create_key(conn, name or f"test-{uuid4().hex[:8]}", role)["key"]
        finally:
            conn.close()

    return _make


@pytest.fixture()
def admin_key(make_key):
    return make_key("admin")


@pytest.fixture()
def client(admin_key):
    """Test client that authenticates every request with an admin key."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.headers["X-API-Key"] = admin_key
        yield c


@pytest.fixture()
def anon_client():
    """Test client with no credentials."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
