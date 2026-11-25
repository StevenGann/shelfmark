"""HTTP API for parts."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from . import auth
from .deps import Conn
from .repo import parts as repo
from .repo.parts import DuplicatePartError

router = APIRouter(
    prefix="/api/parts",
    tags=["parts"],
    dependencies=[Depends(auth.require("viewer"))],
)

DesignStatus = Literal["active", "nrnd", "obsolete", "do-not-use"]


class PartCreate(BaseModel):
    mpn: str
    manufacturer: str
    description: str | None = None
    category: str | None = None
    notes: str | None = None
    design_status: DesignStatus = "active"


class PartUpdate(BaseModel):
    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    category: str | None = None
    notes: str | None = None
    design_status: DesignStatus | None = None

    @field_validator("mpn", "manufacturer", "design_status")
    @classmethod
    def _reject_none(cls, value: object) -> object:
        if value is None:
            raise ValueError("field cannot be null")
        return value


@router.get("")
def list_parts(
    conn: Conn,
    q: str = Query(default=""),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = repo.list_parts(conn, q=q, category=category, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201, dependencies=[Depends(auth.require("engineer"))])
def create_part(
    body: PartCreate,
    conn: Conn,
) -> dict:
    try:
        return repo.create_part(
            conn,
            mpn=body.mpn,
            manufacturer=body.manufacturer,
            description=body.description,
            category=body.category,
            notes=body.notes,
            design_status=body.design_status,
        )
    except DuplicatePartError as e:
        raise HTTPException(status_code=409, detail={"ipn": e.ipn}) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/{ipn}")
def get_part(ipn: str, conn: Conn) -> dict:
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    return part


@router.patch("/{ipn}", dependencies=[Depends(auth.require("engineer"))])
def patch_part(
    ipn: str,
    body: PartUpdate,
    conn: Conn,
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    try:
        part = repo.update_part(conn, ipn, **fields)
    except DuplicatePartError as e:
        raise HTTPException(status_code=409, detail={"ipn": e.ipn}) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    return part
