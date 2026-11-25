import os
import shutil
import tempfile
from uuid import uuid4

import pytest

# Configure scratch directories before any test imports the app: the settings
# object is built at import time.
_BASE_DIR = tempfile.mkdtemp(prefix="shelfmark-test-")
os.environ.setdefault("SHELFMARK_DATA_DIR", _BASE_DIR)
os.environ.setdefault("SHELFMARK_ASSET_DIR", os.path.join(_BASE_DIR, "assets"))


@pytest.fixture(autouse=True)
def reset_db():
    """Reset the mutable tables before each test so tests are independent.

    The data directory is shared across the suite, so parts, users and the IPN
    counter left behind by one test would leak into the next. API keys are left
    alone: each test creates its own unique keys and never relies on an empty
    ``api_key`` table.
    """
    from app import db, ratelimit
    from app.config import settings

    ratelimit.reset_all()
    db.migrate()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM user_session")
        conn.execute("DELETE FROM app_user")
        conn.execute("DELETE FROM part")
        conn.execute("DELETE FROM bom_line")
        conn.execute("DELETE FROM bom_revision")
        conn.execute("DELETE FROM project")
        conn.execute("UPDATE ipn_counter SET last_value = 0 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    shutil.rmtree(settings.asset_dir, ignore_errors=True)
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


DEFAULT_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def make_user():
    """Create a user of a given role and return its username/password/id."""
    from app import db, users

    def _make(role: str = "admin", username: str | None = None, password: str | None = None):
        db.migrate()
        conn = db.connect()
        try:
            name = username or f"user-{uuid4().hex[:8]}"
            pw = password or DEFAULT_PASSWORD
            user = users.create_user(conn, name, pw, role)
            return {"username": name, "password": pw, "id": user["id"], "role": role}
        finally:
            conn.close()

    return _make


@pytest.fixture()
def user_client(make_user):
    """Test client logged in as an admin via a session cookie."""
    from fastapi.testclient import TestClient

    from app.main import app

    user = make_user("admin")
    with TestClient(app) as c:
        r = c.post(
            "/login",
            data={"username": user["username"], "password": user["password"], "next": "/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        c.cookies.set("smk_session", r.cookies["smk_session"])
        c.user = user
        yield c
