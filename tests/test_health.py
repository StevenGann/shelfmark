import os
import tempfile

os.environ.setdefault("SHELFMARK_DATA_DIR", tempfile.mkdtemp(prefix="shelfmark-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_health_reports_db_ok():
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"


def test_migration_is_idempotent():
    from app import db

    assert db.migrate() == []  # lifespan already applied them
    assert db.check() is True
