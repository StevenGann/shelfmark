"""HTTP API for offers and price breaks."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth, enrich, pricing
from .deps import Conn
from .repo import catalog
from .repo import offers as offers_repo
from .repo import parts as parts_repo

router = APIRouter(tags=["offers"])

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]


class BreakIn(BaseModel):
    quantity: int = Field(ge=1)
    unit_price_micros: int = Field(ge=0)


class OfferCreate(BaseModel):
    distributor: str
    sku: str
    packaging: str | None = None
    url: str | None = None
    currency: str = "USD"
    minimum_quantity: int = Field(default=1, ge=1)
    stock_quantity: int | None = None
    breaks: list[BreakIn]


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


@router.get("/api/parts/{ipn}/offers", dependencies=[Depends(auth.require("viewer"))])
def list_offers(
    ipn: str,
    conn: Conn,
    quantity: int = Query(default=1, ge=1),
) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    best = pricing.best_offer(conn, int(part["id"]), quantity)
    return {
        "quantity": quantity,
        "offers": pricing.offers_at(conn, int(part["id"]), quantity),
        "best": best["offer"]["id"] if best else None,
    }


@router.post("/api/parts/{ipn}/offers", status_code=201)
def create_offer(ipn: str, body: OfferCreate, conn: Conn, key: EngineerKey) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    distributor = catalog.get_distributor_by_name_norm(
        conn, catalog.normalise_name(body.distributor)
    )
    if distributor is None:
        raise HTTPException(status_code=422, detail="unknown distributor")
    breaks = [
        {"quantity": b.quantity, "unit_price_micros": b.unit_price_micros} for b in body.breaks
    ]
    try:
        return offers_repo.add_manual_offer(
            conn,
            int(part["id"]),
            distributor_id=int(distributor["id"]),
            sku=body.sku,
            packaging=body.packaging,
            url=body.url,
            currency=body.currency,
            minimum_quantity=body.minimum_quantity,
            stock_quantity=body.stock_quantity,
            breaks=breaks,
            created_by=_api_actor(key),
        )
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="an offer with this distributor and SKU already exists"
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/api/offers/{offer_id}")
def delete_offer(offer_id: int, conn: Conn, key: EngineerKey) -> dict:
    if not offers_repo.delete_offer(conn, offer_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": offer_id}


@router.post("/api/parts/{ipn}/offers/refresh")
def refresh_offers(ipn: str, conn: Conn, key: EngineerKey) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        offers = pricing.refresh_part_offers(conn, part)
    except (
        enrich.CredentialsError,
        enrich.BudgetError,
        enrich.CatalogueMiss,
        enrich.TransientError,
    ) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"refreshed": True, "offers": offers}
