"""DigiKey Product Information API v4 enrichment.

One MPN, one call: ``GET /products/v4/search/{mpn}/productdetails`` returns the
manufacturer, product URL, datasheet URL, photo URL and the full parameter set
(including ``Package / Case``) without any keyword-search ambiguity to resolve.

The HTTP layer is deliberately small and module-level so the tests can swap in
recorded fixtures instead of hitting the live API.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from . import assets
from .config import settings
from .repo import catalog
from .repo import parts as repo

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_API_BASE = "https://api.digikey.com"
DIGIKEY_PRODUCT_DETAILS = "/products/v4/search/{mpn}/productdetails"

DATASHEET_TYPES = {"application/pdf"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

DATASHEET_MAX_BYTES = 20 * 1024 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024

MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 6 * 3600
CREDENTIALS_RETRY_SECONDS = 300

USER_AGENT = "shelfmark/0.1"


class CredentialsError(Exception):
    """DigiKey credentials are missing; the job stays queued."""


class BudgetError(Exception):
    """The daily DigiKey call budget is exhausted."""


class CatalogueMiss(Exception):
    """The MPN is not in the DigiKey catalogue (terminal)."""


class TransientError(Exception):
    """A retriable failure (429, 5xx, network)."""


class DownloadError(Exception):
    """An asset could not be fetched as an acceptable content type/size."""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open(
    url: str,
    timeout: int,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def _get_access_token() -> str:
    payload = urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": settings.digikey_client_id,
            "client_secret": settings.digikey_client_secret,
        }
    ).encode()
    resp = _open(
        DIGIKEY_TOKEN_URL,
        30,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    try:
        token = json.load(resp).get("access_token")
    finally:
        resp.close()
    if not token:
        raise TransientError("DigiKey token response had no access_token")
    return token


def _map_http_error(error: urllib.error.HTTPError) -> Exception:
    code = error.code
    if code == 404:
        return CatalogueMiss("not in the DigiKey catalogue")
    if code == 429:
        return TransientError("DigiKey rate limited (429)")
    if 500 <= code < 600:
        return TransientError(f"DigiKey server error ({code})")
    return TransientError(f"DigiKey request failed ({code})")


def _fetch_product_details_http(mpn: str) -> dict[str, Any]:
    token = _get_access_token()
    url = DIGIKEY_API_BASE + DIGIKEY_PRODUCT_DETAILS.format(mpn=quote(mpn))
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": settings.digikey_client_id,
    }
    try:
        resp = _open(url, 30, headers=headers)
    except urllib.error.HTTPError as error:
        raise _map_http_error(error) from error
    except urllib.error.URLError as error:
        raise TransientError(f"network error: {error}") from error
    try:
        return json.load(resp)
    finally:
        resp.close()


_budget_date = ""
_budget_used = 0
_last_call = 0.0


def _credentials_configured() -> bool:
    return bool(settings.digikey_client_id and settings.digikey_client_secret)


def _budget_ok() -> bool:
    global _budget_date, _budget_used
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if _budget_date != today:
        _budget_date = today
        _budget_used = 0
    return _budget_used < settings.digikey_daily_budget


def _consume_budget() -> None:
    global _budget_used
    _budget_used += 1


def _throttle() -> None:
    global _last_call
    now = time.monotonic()
    if now - _last_call < 1.0:
        time.sleep(1.0 - (now - _last_call))
    _last_call = time.monotonic()


def fetch_product_details(mpn: str) -> dict[str, Any]:
    """Fetch productdetails for one MPN, enforcing credentials, budget and rate."""
    if not _credentials_configured():
        raise CredentialsError("credentials not configured")
    if not _budget_ok():
        raise BudgetError("daily DigiKey budget exhausted")
    _throttle()
    _consume_budget()
    return _fetch_product_details_http(mpn)


def parse_product_details(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields enrichment needs from a productdetails response.

    The live API wraps the record in ``{"Product": {...}}`` and names parameter
    fields ``ParameterText``/``ValueText``. Both shapes are accepted so a caller
    holding a bare record still works, but the fixtures under ``tests/fixtures``
    are verbatim captures, not reconstructions.
    """
    product = data.get("Product") or data
    manufacturer = (product.get("Manufacturer") or {}).get("Name")
    package = None
    for entry in product.get("Parameters") or []:
        if not isinstance(entry, dict):
            continue
        label = entry.get("ParameterText") or entry.get("Parameter")
        if label == "Package / Case":
            package = entry.get("ValueText") or entry.get("Value")
            break
    part_number = product.get("DigiKeyPartNumber")
    if not part_number:
        variants = product.get("ProductVariations") or []
        if variants and isinstance(variants[0], dict):
            part_number = variants[0].get("DigiKeyProductNumber")
    return {
        "manufacturer": manufacturer or "",
        "package": package,
        "datasheet_url": product.get("DatasheetUrl"),
        "photo_url": product.get("PhotoUrl"),
        "product_url": product.get("ProductUrl"),
        "digikey_part_number": part_number,
    }


def digikey_manufacturer_url(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"https://www.digikey.com/en/supplier-centers/{slug}"


def normalise_url(url: str) -> str:
    """Make a supplier URL absolute.

    DigiKey returns some datasheet links protocol-relative
    (``//mm.digikey.com/...``); urllib refuses those outright, so they are
    completed as https rather than treated as a failure.
    """
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def download(
    url: str,
    dest: str,
    *,
    max_bytes: int,
    allowed_types: set[str],
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Download *url* to *dest*; return ``(bytes, sha256, content_type)``.

    Refuses a final (post-redirect) URL that is not http(s), an unexpected
    content type, and a body larger than ``max_bytes`` — all as a recorded
    ``DownloadError`` rather than a guess.
    """
    try:
        resp = _open(normalise_url(url), timeout)
    except urllib.error.HTTPError as error:
        # A missing candidate is a download failure, not a reason to abandon the
        # whole enrichment: the caller tries the next candidate.
        raise DownloadError(f"HTTP {error.code} for {url}") from error
    except urllib.error.URLError as error:
        raise DownloadError(f"network error: {error.reason}") from error
    try:
        final_url = resp.geturl()
        if not final_url.startswith(("http://", "https://")):
            raise DownloadError(f"refused non-http(s) final URL: {final_url}")
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type not in allowed_types:
            raise DownloadError(f"unexpected content type: {content_type or 'unknown'}")
        hasher = hashlib.sha256()
        total = 0
        with open(dest, "wb") as out:
            while True:
                try:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                except (TimeoutError, OSError) as error:
                    # A stalled read is a failed download, not a crashed job.
                    raise DownloadError(f"read failed: {error}") from error
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(f"download exceeds {max_bytes} bytes")
                hasher.update(chunk)
                out.write(chunk)
        return total, hasher.hexdigest(), content_type
    finally:
        resp.close()


def _update_part_identity(
    conn: sqlite3.Connection, part_id: int, manufacturer_id: int, package: str | None
) -> None:
    now = _utc_now()
    if package is not None:
        try:
            conn.execute(
                "UPDATE part SET manufacturer_id = ?, package = ?, updated_at = ? WHERE id = ?",
                (manufacturer_id, package, now, part_id),
            )
            conn.commit()
            return
        except sqlite3.IntegrityError:
            conn.rollback()
    if package is not None:
        conn.execute(
            "UPDATE part SET package = ?, updated_at = ? WHERE id = ?", (package, now, part_id)
        )
        conn.commit()


TI_SYMLINK = "https://www.ti.com/lit/ds/symlink/{mpn}.pdf"


def datasheet_fallbacks(mpn: str, manufacturer: str, failed_url: str) -> list[str]:
    """Candidate datasheet URLs to try after the supplier link fails.

    Some manufacturers hand out a JavaScript redirector instead of a document:
    TI's ``suppproductinfo.tsp`` returns an HTML page that only resolves in a
    browser. These candidates are conventions, not guesses — a candidate is used
    only if it actually returns a PDF that passes the same content-type check.
    """
    candidates: list[str] = []
    if not ("ti.com" in (failed_url or "") or "texas instruments" in (manufacturer or "").lower()):
        return candidates
    # TI names the document after the base part, not the orderable suffix:
    # DAC81416RHAT -> dac81416.pdf. Try the full MPN first, then the base, and
    # let the download itself decide which one exists.
    lowered = mpn.strip().lower()
    match = re.match(r"^[a-z]+[0-9]+", lowered)
    for candidate in (lowered, match.group(0) if match else None):
        if candidate and candidate not in candidates:
            candidates.append(TI_SYMLINK.format(mpn=candidate))
    return candidates


def _fetch_asset(
    conn: sqlite3.Connection,
    part: dict,
    kind: str,
    source_url: str,
    *,
    manufacturer: str = "",
) -> None:
    rel_path = assets.rel_path_for(part["ipn"], kind)
    allowed = DATASHEET_TYPES if kind == "datasheet" else IMAGE_TYPES
    cap = DATASHEET_MAX_BYTES if kind == "datasheet" else IMAGE_MAX_BYTES
    now = _utc_now()
    tmp = assets.tmp_path(rel_path)
    attempts = [source_url]
    if kind == "datasheet":
        attempts += datasheet_fallbacks(part["mpn"], manufacturer, source_url)

    last_error = "no source URL"
    for url in attempts:
        try:
            size, sha256, content_type = download(
                url, str(tmp), max_bytes=cap, allowed_types=allowed
            )
        except DownloadError as error:
            tmp.unlink(missing_ok=True)
            last_error = str(error)
            continue
        # One asset row per (part, kind): a successful fallback replaces a
        # previously recorded failure rather than sitting beside it.
        catalog.delete_part_assets_of_kind(conn, int(part["id"]), kind)
        assets.commit(tmp, rel_path)
        catalog.add_part_asset(
            conn,
            part["id"],
            kind=kind,
            source_url=url,
            rel_path=rel_path,
            content_type=content_type,
            bytes=size,
            sha256=sha256,
            fetched_at=now,
            status="ok",
        )
        return

    catalog.delete_part_assets_of_kind(conn, int(part["id"]), kind)
    catalog.add_part_asset(
        conn,
        part["id"],
        kind=kind,
        source_url=source_url,
        rel_path=rel_path,
        content_type="",
        bytes=0,
        sha256="",
        fetched_at=now,
        status="failed",
        error=last_error,
    )


def run_enrichment(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    part = repo.get_part_by_id(conn, int(job["part_id"]))
    if part is None:
        raise CatalogueMiss(f"part {job['part_id']} no longer exists")

    data = fetch_product_details(part["mpn"])
    parsed = parse_product_details(data)
    if not parsed["manufacturer"]:
        raise TransientError("productdetails response had no manufacturer")

    manufacturer = catalog.upsert_manufacturer(conn, parsed["manufacturer"])
    _update_part_identity(conn, part["id"], int(manufacturer["id"]), parsed["package"])

    now = _utc_now()
    mfr_url = manufacturer["website"] or digikey_manufacturer_url(manufacturer["name"])
    catalog.add_part_link(
        conn,
        part["id"],
        kind="manufacturer",
        label=manufacturer["name"],
        url=mfr_url,
        source="digikey",
        fetched_at=now,
    )

    if parsed["product_url"]:
        digikey = catalog.get_distributor_by_name_norm(conn, "digikey")
        catalog.add_part_link(
            conn,
            part["id"],
            kind="distributor",
            label="DigiKey",
            url=parsed["product_url"],
            distributor_id=int(digikey["id"]) if digikey else None,
            sku=parsed["digikey_part_number"],
            source="digikey",
            fetched_at=now,
        )

    for distributor in catalog.list_distributors(conn):
        if distributor["name_norm"] == "digikey":
            continue
        template = distributor["search_url_template"]
        if not template:
            continue
        catalog.add_part_link(
            conn,
            part["id"],
            kind="distributor",
            label=distributor["name"],
            url=template.replace("{mpn}", part["mpn"]),
            distributor_id=int(distributor["id"]),
            source="template",
        )

    if parsed["datasheet_url"]:
        catalog.add_part_link(
            conn,
            part["id"],
            kind="datasheet",
            label="Datasheet",
            url=parsed["datasheet_url"],
            source="digikey",
            fetched_at=now,
        )
        _fetch_asset(
            conn, part, "datasheet", parsed["datasheet_url"], manufacturer=parsed["manufacturer"]
        )

    if parsed["photo_url"]:
        _fetch_asset(conn, part, "image", parsed["photo_url"])


def _backoff_seconds(attempts: int) -> int:
    return min(RETRY_BASE_SECONDS * (2 ** (attempts - 1)), RETRY_MAX_SECONDS)


def _later(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_or_give_up(conn: sqlite3.Connection, job_id: int, attempts: int, message: str) -> None:
    """Retry with backoff, but stop retrying a part that has never worked.

    Without a cap, one host that always stalls is retried forever and holds a
    slot in every queue drain. Environment problems (credentials, budget) do
    not pass through here: they are not the part's fault.
    """
    if attempts >= MAX_ATTEMPTS:
        catalog.mark_failed(conn, job_id, f"gave up after {attempts} attempts: {message}")
        return
    catalog.mark_retry(conn, job_id, message, _later(_backoff_seconds(attempts)))


def _next_day() -> str:
    return (
        (datetime.now(UTC) + timedelta(days=1))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def run_queue(conn: sqlite3.Connection) -> int:
    """Drain due queued jobs in-process. Returns how many were attempted."""
    # A job left in "running" belongs to a process that died mid-fetch; nothing
    # else is holding it, so it goes back in the queue rather than sitting there
    # invisible forever.
    catalog.requeue_stale_running(conn)
    processed = 0
    for job in catalog.list_due_jobs(conn):
        processed += 1
        catalog.mark_running(conn, int(job["id"]))
        attempts = int(job["attempts"]) + 1
        try:
            run_enrichment(conn, job)
        except CredentialsError as error:
            catalog.mark_retry(conn, int(job["id"]), str(error), _later(CREDENTIALS_RETRY_SECONDS))
        except BudgetError as error:
            catalog.mark_retry(conn, int(job["id"]), str(error), _next_day())
        except CatalogueMiss as error:
            catalog.mark_failed(conn, int(job["id"]), str(error))
        except TransientError as error:
            _retry_or_give_up(conn, int(job["id"]), attempts, str(error))
        except Exception as error:  # noqa: BLE001 - the queue must never die on one part
            _retry_or_give_up(conn, int(job["id"]), attempts, f"unexpected error: {error}")
        else:
            catalog.mark_done(conn, int(job["id"]))
    return processed
