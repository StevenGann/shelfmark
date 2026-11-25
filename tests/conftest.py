import os
import tempfile

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
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
