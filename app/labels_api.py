"""HTTP API for label capture.

Both endpoints are ``engineer``. Decode writes nothing; receive re-decodes the
upload server-side (the client's decoded fields are never trusted) and records a
``receive`` stock move whose note carries the lot and date code.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import auth, labels
from .deps import Conn
from .repo import stock as stock_repo
from .repo.parts import DuplicatePartError
from .repo.stock import StockError

router = APIRouter(tags=["labels"])

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _label_error(exc: labels.LabelError) -> HTTPException:
    detail: dict = {"message": str(exc)}
    if exc.payloads:
        detail["payloads"] = exc.payloads
    return HTTPException(status_code=422, detail=detail)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"message": "Quantity must be a whole number."}
        ) from None


@router.post("/api/labels/decode")
async def decode_label(request: Request, conn: Conn, key: EngineerKey) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail={"message": "file is required"})
    try:
        data = await labels.read_upload(upload)
    except labels.LabelError as exc:
        raise _label_error(exc) from None
    try:
        payloads, chosen = labels.decode(data)
    except labels.LabelError as exc:
        raise _label_error(exc) from None
    parsed = labels.parse_label(chosen, payloads)
    if not parsed["mpn"] and not parsed["distributor_pn"]:
        raise _label_error(
            labels.LabelError(
                "No barcode found in that photo — try again with the label filling the frame.",
                payloads=payloads,
            )
        )
    return {
        "payloads": payloads,
        "label": parsed,
        "resolution": labels.resolve_label(conn, parsed),
    }


@router.post("/api/labels/receive", status_code=201)
async def receive_label(request: Request, conn: Conn, key: EngineerKey) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail={"message": "file is required"})
    try:
        data = await labels.read_upload(upload)
    except labels.LabelError as exc:
        raise _label_error(exc) from None
    try:
        payloads, chosen = labels.decode(data)
    except labels.LabelError as exc:
        raise _label_error(exc) from None
    parsed = labels.parse_label(chosen, payloads)
    resolution = labels.resolve_label(conn, parsed)
    part = resolution["part"]

    if part is None:
        if not _truthy(str(form.get("create_part") or "")):
            raise HTTPException(
                status_code=422,
                detail={"message": "The label does not match an existing part."},
            )
        mfr = (str(form.get("manufacturer") or "")).strip()
        if not mfr:
            raise HTTPException(
                status_code=422,
                detail={"message": "Manufacturer is required to create a part."},
            )
        try:
            part = labels.create_part_from_label(conn, parsed, mfr)
        except DuplicatePartError as exc:
            raise HTTPException(
                status_code=409, detail={"message": str(exc), "ipn": exc.ipn}
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"message": str(exc)}) from exc

    quantity = _optional_int(form.get("quantity"))
    qty = quantity if quantity is not None else (parsed.get("quantity") or 1)
    note_value = form.get("note")
    note = str(note_value) if note_value else None
    move_note = "; ".join(p for p in (labels.lot_note(parsed), note) if p) or None
    location = str(form.get("location") or "")
    try:
        move = stock_repo.record_move(
            conn,
            int(part["id"]),
            location,
            "receive",
            int(qty),
            move_note,
            _api_actor(key),
        )
    except (StockError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)}) from exc
    return {"part": part, "move": move, "label": parsed}
