"""Decode and resolve DigiKey shipping labels.

Every DigiKey bag carries a Data Matrix encoded as an ANSI MH10.8.2 message:
``[)>`` + RS, then GS-separated fields of *application identifier* + value. A
single photo may also carry linear Code 128 barcodes that are DigiKey-internal
references; those are read but never used for matching.

Decoding uses ``zxing-cpp`` (with Pillow for the image side). Resolution maps a
parsed label to an existing part by DigiKey part number, then by MPN, and falls
back to an unknown-part create form. Nothing here writes to the database except
:func:`create_part_from_label`, which exists so the API and CLI share the
"create a part from a scan" path.
"""

from __future__ import annotations

import io
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

import zxingcpp
from PIL import Image

from .mpn import normalise_mpn
from .repo import catalog
from .repo import parts as parts_repo
from .repo import stock as stock_repo
from .repo.catalog import normalise_name

MAX_IMAGE_BYTES = 20 * 1024 * 1024

# The linear barcodes on the same label are DigiKey-internal references; only a
# payload with one of these suffixes is a real DigiKey part number.
_DIGIKEY_PN_SUFFIXES = ("-ND", "-CT", "-TR", "-DKR")

_JPEG_SIGNATURE = b"\xff\xd8\xff"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_FORMATS = zxingcpp.BarcodeFormats(
    (
        zxingcpp.BarcodeFormat.DataMatrix,
        zxingcpp.BarcodeFormat.Code128,
        zxingcpp.BarcodeFormat.QRCode,
        zxingcpp.BarcodeFormat.Code39,
        zxingcpp.BarcodeFormat.PDF417,
    )
)

# MH10.8.2 data identifiers, longest first so that ``10K`` wins over ``K`` and
# ``1K`` over ``K``. The trailing entries (``1K``, ``11K``, ``11Z``, …) are
# DigiKey-specific and preserved in ``fields`` rather than dropped.
_MH10_IDS = (
    "30P",
    "14K",
    "10K",
    "11K",
    "11Z",
    "12Z",
    "13Z",
    "20Z",
    "06",
    "1P",
    "1T",
    "1V",
    "1K",
    "4L",
    "9D",
    "Q",
    "K",
    "P",
)

# Named AIs that become first-class fields rather than landing in ``fields``.
_NAMED_IDS = {"30P", "06", "1P", "1V", "K", "Q", "9D", "1T", "4L", "10K"}

# DigiKey part numbers end in one of these; Mouser's do not. Used to tell a
# supplier part number from a manufacturer part number when a label omits ``1P``.
_DIGIKEY_SUFFIXES = ("-ND", "-CT", "-TR", "-DKR")


class LabelError(Exception):
    """A label image cannot be read, or carries no usable symbol."""

    def __init__(self, message: str, payloads: list[str] | None = None) -> None:
        super().__init__(message)
        self.payloads = payloads or []


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def magic_type(head: bytes) -> str | None:
    """Return the image format for a magic-byte sniff, or ``None``."""
    if head.startswith(_JPEG_SIGNATURE):
        return "jpeg"
    if head.startswith(_PNG_SIGNATURE):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _prepare_image(image: Image.Image) -> Image.Image:
    """Downscale a phone photo before decoding, so big frames stay fast."""
    width, height = image.size
    if max(width, height) <= 2400:
        return image
    scale = 0.4
    return image.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )


def _payload_text(result: zxingcpp.Barcode) -> str:
    """The raw symbol payload, preserving MH10.8.2 RS/GS control bytes."""
    data = result.bytes
    if isinstance(data, (bytes, bytearray)):
        raw = bytes(data)
    else:
        raw = str(result.text).encode("latin-1", "replace")
    return raw.decode("latin-1")


def _supplier_pn(text: str) -> tuple[str, str] | None:
    """Split a Code 128 payload into ``(supplier_pn, derived_mpn)``.

    Mouser prints ``<3-digit mfr id>-<manufacturer part number>`` in the linear
    code and, on some labels, omits the MPN from the Data Matrix entirely. A
    DigiKey part number has the same shape but carries a packaging suffix, so
    those are rejected: stripping three digits off ``296-OPA211AIDRGRCT-ND``
    would invent a part number that does not exist.
    """
    text = text.strip()
    match = re.match(r"^(\d{3})-([A-Za-z0-9].*)$", text)
    if match is None:
        return None
    if any(text.upper().endswith(suffix) for suffix in _DIGIKEY_SUFFIXES):
        return None
    return text, match.group(2)


def _looks_like_digikey_pn(text: str) -> bool:
    upper = text.strip().upper()
    return any(upper.endswith(suffix) for suffix in _DIGIKEY_PN_SUFFIXES)


def _choose_payload(results: list[zxingcpp.Barcode]) -> str | None:
    """Prefer the MH10.8.2 Data Matrix; fall back to a DigiKey-shaped Code 128."""
    data_matrix_candidates = [r for r in results if r.format == zxingcpp.BarcodeFormat.DataMatrix]
    for result in data_matrix_candidates:
        text = _payload_text(result)
        if text.startswith("[)>"):
            return text
    for result in results:
        if result.format == zxingcpp.BarcodeFormat.Code128:
            text = _payload_text(result)
            if _looks_like_digikey_pn(text):
                return text
    return None


def decode(image_bytes: bytes) -> tuple[list[str], str]:
    """Return ``(payloads, chosen)`` for a JPEG/PNG/WebP image.

    ``payloads`` is every raw symbol read; ``chosen`` is the payload selected
    for matching. Raises :class:`LabelError` when the bytes are not a supported
    image, exceed the size cap, or carry no usable symbol (the payloads that
    *were* read ride along on the error for the 422 response).
    """
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise LabelError("Image exceeds the 20 MB limit.")
    if magic_type(image_bytes) is None:
        raise LabelError("Expected a JPEG, PNG or WebP image.")
    try:
        image: Image.Image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        raise LabelError("The image could not be read.") from None
    image = _prepare_image(image)
    results = zxingcpp.read_barcodes(
        image,
        formats=_FORMATS,
        try_rotate=True,
        try_downscale=True,
        try_invert=True,
        try_denoise=True,
    )
    payloads = [_payload_text(r) for r in results]
    chosen = _choose_payload(results)
    if chosen is None and not payloads:
        raise LabelError(
            "No barcode found in that photo — try again with the label filling the frame.",
            payloads=payloads,
        )
    # No MH10.8.2 symbol, but the linear codes read: a small or angled label is
    # still worth prefilling from them. ``parse_label("", payloads)`` assembles
    # what it can and the human confirms before anything is written.
    return payloads, chosen or ""


def _split_mh10(raw: str) -> dict[str, str]:
    body = raw[3:] if raw.startswith("[)>") else raw
    body = body.lstrip("\x1e")
    fields: dict[str, str] = {}
    for token in body.split("\x1d"):
        # A real label ends the message with RS + EOT, which would otherwise
        # ride along on the last field's value.
        token = token.strip().strip("\x1e\x04").strip()
        if not token:
            continue
        for ai in _MH10_IDS:
            if token.startswith(ai):
                fields[ai] = token[len(ai) :]
                break
    return fields


def _to_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_linear(payloads: list[str], extras: list[str], result: dict) -> dict:
    """Assemble a label from linear codes when the 2D symbol did not read.

    Mouser's linear payloads are ``<3-digit mfr id>-<mpn>``, the bare MPN, the
    quantity, a line item, the origin and an invoice. The supplier part number
    and the MPN come from the dotted code; the quantity is the largest short
    integer (line items are three digits at most). Nothing is written from
    these guesses without a human confirming the form.
    """
    for payload in list(payloads) + list(extras):
        payload = payload.strip()
        if not payload:
            continue
        split = _supplier_pn(payload)
        if split is not None:
            supplier_pn, derived_mpn = split
            if result["distributor_pn"] is None:
                result["distributor_pn"] = supplier_pn
            if result["mpn"] is None:
                result["mpn"] = derived_mpn
            if result["supplier"] is None:
                result["supplier"] = "Mouser"
        if result["distributor_pn"] is None and _looks_like_digikey_pn(payload):
            result["distributor_pn"] = payload
            result["supplier"] = "DigiKey"
    numbers = [
        int(p)
        for p in list(payloads) + list(extras)
        if p.strip().isdigit() and len(p.strip()) <= 6 and int(p) > 0
    ]
    if result["quantity"] is None and numbers:
        result["quantity"] = max(numbers)
    if result["country"] is None:
        for payload in list(payloads) + list(extras):
            if re.fullmatch(r"[A-Z]{2}", payload.strip()):
                result["country"] = payload.strip()
                break
    return result


def parse_label(raw: str, extras: list[str] | None = None) -> dict:
    """Parse a decoded payload into the label dict.

    MH10.8.2 payloads map to named fields. ``extras`` are the other symbols read
    from the same photo: Mouser omits ``1P`` on some labels, so the manufacturer
    part number is recovered from the linear ``<mfr id>-<mpn>`` code when the
    2D symbol does not carry one. Unknown AIs are preserved in ``fields``.
    """
    result: dict = {
        "distributor_pn": None,
        "mpn": None,
        "manufacturer": None,
        "description": None,
        "quantity": None,
        "lot": None,
        "date_code": None,
        "country": None,
        "purchase_order": None,
        "supplier": None,
        "fields": {},
        "raw": raw,
    }
    stripped = raw.strip()
    if not stripped.startswith("[)>"):
        return _parse_linear([stripped] if stripped else [], extras or [], result)

    fields = _split_mh10(stripped)
    # DigiKey never sends ``1V``; Mouser always does. ``06`` is empty on both.
    result["supplier"] = "Mouser" if fields.get("1V") else "DigiKey"
    if fields.get("30P"):
        result["distributor_pn"] = fields["30P"]
    elif fields.get("06"):
        result["distributor_pn"] = fields["06"].removeprefix("P")
    elif fields.get("P") and "," not in fields["P"] and len(fields["P"]) >= 5:
        # DigiKey puts its part number in ``P``. On Mouser labels ``P`` is the
        # position list ("6-8,11,13-4"), which is never a part number.
        result["distributor_pn"] = fields["P"].removeprefix("P")
    if "1P" in fields:
        result["mpn"] = fields["1P"]
    if "1V" in fields:
        result["manufacturer"] = fields["1V"].strip()
    if "K" in fields:
        result["description"] = fields["K"]
    if "Q" in fields:
        result["quantity"] = _to_int(fields["Q"])
    if "9D" in fields:
        result["date_code"] = fields["9D"]
    if "1T" in fields:
        result["lot"] = fields["1T"]
    if "4L" in fields:
        result["country"] = fields["4L"]
    if "10K" in fields:
        result["purchase_order"] = fields["10K"]

    # Fill the gaps from the linear codes when the 2D symbol is incomplete.
    for payload in extras or []:
        split = _supplier_pn(payload)
        if split is None:
            continue
        supplier_pn, derived_mpn = split
        if result["distributor_pn"] is None:
            result["distributor_pn"] = supplier_pn
        if result["mpn"] is None:
            result["mpn"] = derived_mpn

    for ai, value in fields.items():
        if ai not in _NAMED_IDS:
            result["fields"][ai] = value
    return result


def _part_by_distributor_pn(conn: sqlite3.Connection, distributor_pn: str) -> dict | None:
    row = conn.execute(
        "SELECT p.id FROM part_link pl "
        "JOIN part p ON p.id = pl.part_id "
        "WHERE pl.sku = ? ORDER BY pl.id LIMIT 1",
        (distributor_pn,),
    ).fetchone()
    if row is None:
        return None
    return parts_repo.get_part_by_id(conn, int(row["id"]))


def _part_by_mpn(conn: sqlite3.Connection, mpn: str) -> dict | None:
    norm = normalise_mpn(mpn)
    rows = conn.execute("SELECT id FROM part WHERE mpn_norm = ?", (norm,)).fetchall()
    if len(rows) != 1:
        return None
    return parts_repo.get_part_by_id(conn, int(rows[0]["id"]))


def _suggested_location(conn: sqlite3.Connection, part_id: int) -> str | None:
    row = conn.execute(
        "SELECT si.location FROM stock_move sm "
        "JOIN stock_item si ON si.id = sm.stock_item_id "
        "WHERE si.part_id = ? AND sm.kind = 'receive' ORDER BY sm.id DESC LIMIT 1",
        (part_id,),
    ).fetchone()
    return row["location"] if row is not None else None


def resolve_label(conn: sqlite3.Connection, parsed: dict) -> dict:
    """Answer "what is this, and where does it live".

    Resolution is by supplier part number first (``part_link.sku`` on any
    distributor), then by MPN (exactly one part or none), then unknown.
    """
    distributor_pn = parsed.get("distributor_pn")
    mpn = parsed.get("mpn")
    part: dict | None = None
    matched_by: str | None = None
    if distributor_pn:
        part = _part_by_distributor_pn(conn, distributor_pn)
        if part is not None:
            matched_by = "distributor_pn"
    if part is None and mpn:
        part = _part_by_mpn(conn, mpn)
        if part is not None:
            matched_by = "mpn"
    if part is None:
        return {
            "status": "unknown",
            "matched_by": None,
            "part": None,
            "suggested_location": None,
            "stock": [],
        }
    return {
        "status": "matched",
        "matched_by": matched_by,
        "part": part,
        "suggested_location": _suggested_location(conn, int(part["id"])),
        "stock": stock_repo.get_stock(conn, int(part["id"])),
    }


def lot_note(parsed: dict) -> str | None:
    """The traceability note for a receive: ``lot <lot>, DC <date_code>``."""
    pieces = []
    if parsed.get("lot"):
        pieces.append(f"lot {parsed['lot']}")
    if parsed.get("date_code"):
        pieces.append(f"DC {parsed['date_code']}")
    return ", ".join(pieces) or None


def _store_distributor_link(
    conn: sqlite3.Connection,
    part_id: int,
    supplier: str | None,
    sku: str,
) -> None:
    """Record a supplier part number so the next scan of this bag resolves.

    The distributor is matched by name; an unknown supplier falls back to
    DigiKey's row only when the part number looks like DigiKey's, otherwise the
    link is stored without a distributor rather than under the wrong one.
    """
    name_norm = normalise_name(supplier) if supplier else None
    distributor = catalog.get_distributor_by_name_norm(conn, name_norm) if name_norm else None
    url = f"https://www.digikey.com/en/products/result?keywords={sku}"
    if distributor and distributor["search_url_template"]:
        url = str(distributor["search_url_template"]).replace("{mpn}", sku)
    catalog.add_part_link(
        conn,
        part_id,
        kind="distributor",
        label=(distributor["name"] if distributor else (supplier or "Supplier")),
        url=url,
        distributor_id=int(distributor["id"]) if distributor else None,
        sku=sku,
        source="label",
        fetched_at=_utc_now(),
    )


def create_part_from_label(conn: sqlite3.Connection, parsed: dict, manufacturer: str) -> dict:
    """Create a part from a scan and record its supplier part number."""
    mpn = (parsed.get("mpn") or "").strip()
    if not mpn:
        raise ValueError("The label has no manufacturer part number to create.")
    part = parts_repo.create_part(
        conn,
        mpn=mpn,
        manufacturer=manufacturer,
        description=parsed.get("description"),
    )
    distributor_pn = parsed.get("distributor_pn")
    if distributor_pn:
        _store_distributor_link(
            conn,
            int(part["id"]),
            parsed.get("supplier") or parsed.get("manufacturer"),
            distributor_pn,
        )
    return part


async def read_upload(upload: Any) -> bytes:
    """Stream an uploaded image, checking magic bytes and the size cap as it goes.

    ``Content-Length`` is a claim, so the cap is enforced on the bytes that
    actually arrive rather than on the header.
    """
    chunk_size = 64 * 1024
    head = await upload.read(16)
    if not head:
        raise LabelError("The uploaded file is empty.")
    if magic_type(head) is None:
        raise LabelError("Expected a JPEG, PNG or WebP image.")
    data = bytearray(head)
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_IMAGE_BYTES:
            raise LabelError("Image exceeds the 20 MB limit.")
    return bytes(data)
