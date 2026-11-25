import hashlib
import json
from pathlib import Path

import pytest

from app import enrich

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_product_details_ic():
    parsed = enrich.parse_product_details(_load("productdetails_dac81416.json"))
    assert parsed["manufacturer"] == "Texas Instruments"
    assert parsed["package"] == "40-VFQFN Exposed Pad"
    assert "/DAC81416RHAT/" in parsed["product_url"]
    assert "dac81416" in parsed["datasheet_url"]
    assert parsed["digikey_part_number"].startswith("296-53025-")


def test_parse_product_details_passive():
    parsed = enrich.parse_product_details(_load("productdetails_grm155.json"))
    assert parsed["manufacturer"] == "Murata Electronics"
    assert parsed["package"] == "0402 (1005 Metric)"


def test_protocol_relative_datasheet_url_is_made_absolute():
    """DigiKey really does return ``//mm.digikey.com/...`` for some datasheets."""
    parsed = enrich.parse_product_details(_load("productdetails_grm155.json"))
    assert parsed["datasheet_url"].startswith("//")
    assert enrich.normalise_url(parsed["datasheet_url"]).startswith("https://mm.digikey.com/")


def test_download_follows_a_protocol_relative_url(monkeypatch, tmp_path):
    seen = {}

    def fake_open(url, timeout):
        seen["url"] = url
        return FakeResponse(b"%PDF-1.4 x", "application/pdf")

    monkeypatch.setattr(enrich, "_open", fake_open)
    enrich.download(
        "//mm.digikey.com/x.pdf",
        str(tmp_path / "x.pdf"),
        max_bytes=1024,
        allowed_types={"application/pdf"},
    )
    assert seen["url"] == "https://mm.digikey.com/x.pdf"


class FakeResponse:
    def __init__(self, content: bytes, content_type: str, final_url: str = "https://example.com/x"):
        self._content = content
        self.headers = {"Content-Type": content_type}
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url

    def read(self, n: int = -1) -> bytes:
        if n == -1:
            data = self._content
            self._content = b""
            return data
        data = self._content[:n]
        self._content = self._content[n:]
        return data

    def close(self) -> None:
        pass


def test_download_accepts_pdf_and_jpeg(monkeypatch, tmp_path):
    pdf = b"%PDF-1.4 fake datasheet"
    monkeypatch.setattr(enrich, "_open", lambda url, timeout: FakeResponse(pdf, "application/pdf"))
    dest = tmp_path / "d.pdf"
    size, sha256, content_type = enrich.download(
        "https://example.com/d.pdf", str(dest), max_bytes=1024, allowed_types={"application/pdf"}
    )
    assert content_type == "application/pdf"
    assert size == len(pdf)
    assert sha256 == hashlib.sha256(pdf).hexdigest()
    assert dest.read_bytes() == pdf

    jpeg = b"\xff\xd8\xff\xe0fakejpeg"
    monkeypatch.setattr(enrich, "_open", lambda url, timeout: FakeResponse(jpeg, "image/jpeg"))
    dest2 = tmp_path / "i.jpg"
    _, _, content_type = enrich.download(
        "https://example.com/i.jpg", str(dest2), max_bytes=1024, allowed_types=enrich.IMAGE_TYPES
    )
    assert content_type == "image/jpeg"
    assert dest2.read_bytes() == jpeg


def test_download_rejects_html(monkeypatch, tmp_path):
    monkeypatch.setattr(
        enrich, "_open", lambda url, timeout: FakeResponse(b"<html></html>", "text/html")
    )
    with pytest.raises(enrich.DownloadError):
        enrich.download(
            "https://example.com/x",
            str(tmp_path / "x.pdf"),
            max_bytes=1024,
            allowed_types={"application/pdf"},
        )


def _fake_download(url: str, dest: str, *, max_bytes: int, allowed_types, timeout: int = 30):
    if "pdf" in url:
        content = b"%PDF-1.4 fake datasheet"
        content_type = "application/pdf"
    else:
        content = b"\xff\xd8\xff\xe0fakejpeg"
        content_type = "image/jpeg"
    with open(dest, "wb") as handle:
        handle.write(content)
    return len(content), hashlib.sha256(content).hexdigest(), content_type


def test_enrich_is_idempotent(monkeypatch):
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    data = _load("productdetails_dac81416.json")
    monkeypatch.setattr(enrich, "fetch_product_details", lambda mpn: data)
    monkeypatch.setattr(enrich, "download", _fake_download)

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="DAC81416RHAT", manufacturer="Texas Instruments")
    conn.close()

    conn = db.connect()
    catalog.queue_job(conn, int(part["id"]))
    conn.close()

    conn = db.connect()
    enrich.run_queue(conn)
    conn.close()

    conn = db.connect()
    links = catalog.list_part_links(conn, int(part["id"]))
    assets = catalog.list_part_assets(conn, int(part["id"]))
    conn.close()
    assert len(links) > 0
    assert len(assets) == 2

    conn = db.connect()
    catalog.queue_job(conn, int(part["id"]))
    conn.close()

    conn = db.connect()
    enrich.run_queue(conn)
    conn.close()

    conn = db.connect()
    links_again = catalog.list_part_links(conn, int(part["id"]))
    assets_again = catalog.list_part_assets(conn, int(part["id"]))
    part_again = repo.get_part(conn, part["ipn"])
    conn.close()

    assert len(links_again) == len(links)
    assert len(assets_again) == len(assets)
    assert part_again["package"] == "40-VFQFN Exposed Pad"


def test_queued_job_without_credentials_stays_queued(monkeypatch):
    from app import db
    from app.config import settings
    from app.repo import catalog
    from app.repo import parts as repo

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="LM358N", manufacturer="TI")
    job_id = catalog.queue_job(conn, int(part["id"]))
    conn.close()

    original_id = settings.digikey_client_id
    original_secret = settings.digikey_client_secret
    settings.digikey_client_id = ""
    settings.digikey_client_secret = ""
    try:
        conn = db.connect()
        enrich.run_queue(conn)
        conn.close()
    finally:
        settings.digikey_client_id = original_id
        settings.digikey_client_secret = original_secret

    conn = db.connect()
    job = catalog.get_job(conn, job_id)
    conn.close()
    assert job["status"] == "queued"
    assert "credentials not configured" in job["last_error"]


def test_enrich_endpoint_queues_job(client):
    from app import db
    from app.repo import catalog

    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    r = client.post(f"/api/parts/{part['ipn']}/enrich")
    assert r.status_code == 202

    conn = db.connect()
    job = catalog.get_job(conn, int(r.json()["job_id"]))
    conn.close()
    assert job["status"] == "queued"
    assert job["part_id"] == part["id"]


def test_datasheet_fallback_is_used_only_when_verified(monkeypatch):
    """The TI link is a JS redirector; the symlink convention is tried and used."""
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    data = _load("productdetails_dac81416.json")
    monkeypatch.setattr(enrich, "fetch_product_details", lambda mpn: data)

    seen_urls: list[str] = []

    def fake_download(url, dest, *, max_bytes, allowed_types, timeout=30):
        seen_urls.append(url)
        if "suppproductinfo" in url:
            raise enrich.DownloadError("unexpected content type: text/html")
        if not url.endswith("/dac81416.pdf"):
            # The orderable suffix really does 404 on ti.com.
            raise enrich.DownloadError("DigiKey request failed (404)")
        content = b"%PDF-1.4 real-ish"
        with open(dest, "wb") as handle:
            handle.write(content)
        return len(content), hashlib.sha256(content).hexdigest(), "application/pdf"

    monkeypatch.setattr(enrich, "download", fake_download)

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="DAC81416RHAT", manufacturer="Texas Instruments")
    catalog.queue_job(conn, int(part["id"]))
    enrich.run_queue(conn)
    assets = catalog.list_part_assets(conn, int(part["id"]))
    conn.close()

    datasheet = [a for a in assets if a["kind"] == "datasheet"]
    assert len(datasheet) == 1
    assert datasheet[0]["status"] == "ok"
    assert datasheet[0]["source_url"] == "https://www.ti.com/lit/ds/symlink/dac81416.pdf"
    assert seen_urls[0].startswith("https://www.ti.com/general/docs/suppproductinfo")
    assert "https://www.ti.com/lit/ds/symlink/dac81416.pdf" in seen_urls


def test_datasheet_fallbacks_offers_nothing_for_other_manufacturers():
    assert (
        enrich.datasheet_fallbacks("GRM155R71C104KA88D", "Murata Electronics", "https://x/pdf")
        == []
    )
    assert enrich.datasheet_fallbacks("DAC81416RHAT", "Texas Instruments", "") == [
        "https://www.ti.com/lit/ds/symlink/dac81416rhat.pdf",
        "https://www.ti.com/lit/ds/symlink/dac81416.pdf",
    ]


def test_download_reports_http_404_as_a_download_error(monkeypatch, tmp_path):
    import urllib.error

    def fake_open(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(enrich, "_open", fake_open)
    with pytest.raises(enrich.DownloadError):
        enrich.download(
            "https://example.com/missing.pdf",
            str(tmp_path / "x.pdf"),
            max_bytes=1024,
            allowed_types={"application/pdf"},
        )


def test_failed_datasheet_records_failure_without_raising(monkeypatch):
    """All candidates failing must record one failed row, not crash the job."""
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    data = _load("productdetails_dac81416.json")
    monkeypatch.setattr(enrich, "fetch_product_details", lambda mpn: data)

    def always_fails(url, dest, *, max_bytes, allowed_types, timeout=120):
        raise enrich.DownloadError("unexpected content type: text/html")

    monkeypatch.setattr(enrich, "download", always_fails)

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="DAC81416RHAT", manufacturer="Texas Instruments")
    catalog.queue_job(conn, int(part["id"]))
    enrich.run_queue(conn)
    assets = [
        a for a in catalog.list_part_assets(conn, int(part["id"])) if a["kind"] == "datasheet"
    ]
    job = catalog.latest_job(conn, int(part["id"]))
    conn.close()

    assert len(assets) == 1
    assert assets[0]["status"] == "failed"
    assert "text/html" in assets[0]["error"]
    assert job["status"] == "done"


def test_retries_stop_after_the_cap():
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="STALL-1", manufacturer="Nobody")
    catalog.queue_job(conn, int(part["id"]))
    job_id = int(catalog.latest_job(conn, int(part["id"]))["id"])

    enrich._retry_or_give_up(conn, job_id, 1, "read failed")
    assert catalog.latest_job(conn, int(part["id"]))["status"] == "queued"

    enrich._retry_or_give_up(conn, job_id, enrich.MAX_ATTEMPTS, "read failed")
    final = catalog.latest_job(conn, int(part["id"]))
    conn.close()

    assert final["status"] == "failed"
    assert "gave up after" in final["last_error"]


def test_a_job_stuck_running_is_requeued():
    """A process that died mid-fetch must not hide its job forever."""
    from app import db
    from app.repo import catalog
    from app.repo import parts as repo

    db.migrate()
    conn = db.connect()
    part = repo.create_part(conn, mpn="STUCK-1", manufacturer="Nobody")
    catalog.queue_job(conn, int(part["id"]))
    job_id = int(catalog.latest_job(conn, int(part["id"]))["id"])
    catalog.mark_running(conn, job_id)

    assert catalog.requeue_stale_running(conn) == 1
    job = catalog.latest_job(conn, int(part["id"]))
    conn.close()

    assert job["status"] == "queued"
