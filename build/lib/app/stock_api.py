"""HTTP API for stock levels and the stock ledger."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth
from .deps import Conn
from .repo import parts as parts_repo
from .repo import stock as stock_repo
from .repo.stock import StockError

router = APIRouter(tags=["stock"])

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]

MoveKind = Literal["receive", "consume", "adjust", "scrap", "build"]


class StockMoveIn(BaseModel):
    location: str
    kind: MoveKind
    quantity: int = Field(ge=0)
    note: str | None = None


class StockMoveBetweenIn(BaseModel):
    ipn: str
    from_location: str
    to_location: str
    quantity: int = Field(ge=1)


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


@router.get("/api/parts/{ipn}/stock", dependencies=[Depends(auth.require("viewer"))])
def get_part_stock(ipn: str, conn: Conn) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    items = stock_repo.get_stock(conn, int(part["id"]))
    return {
        "total": sum(i["quantity"] for i in items),
        "items": [
            {"location": i["location"], "quantity": i["quantity"], "updated_at": i["updated_at"]}
            for i in items
        ],
    }


@router.post("/api/parts/{ipn}/stock/moves", status_code=201)
def record_stock_move(
    ipn: str,
    body: StockMoveIn,
    conn: Conn,
    key: EngineerKey,
) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        return stock_repo.record_move(
            conn,
            int(part["id"]),
            body.location,
            body.kind,
            body.quantity,
            body.note,
            _api_actor(key),
        )
    except StockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/stock", dependencies=[Depends(auth.require("viewer"))])
def list_stock(
    conn: Conn,
    location: str | None = Query(default=None),
    q: str = Query(default=""),
    below: int | None = Query(default=None, ge=0),
) -> dict:
    rows = stock_repo.list_stock(conn, location=location, q=q or None, below=below)
    return {"items": rows}


@router.get("/api/locations", dependencies=[Depends(auth.require("viewer"))])
def list_locations(conn: Conn) -> dict:
    return {"items": stock_repo.list_locations(conn)}


@router.post("/api/stock/move")
def move_stock(
    body: StockMoveBetweenIn,
    conn: Conn,
    key: EngineerKey,
) -> dict:
    part = parts_repo.get_part(conn, body.ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        return stock_repo.move_between(
            conn,
            int(part["id"]),
            body.from_location,
            body.to_location,
            body.quantity,
            _api_actor(key),
        )
    except StockError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
