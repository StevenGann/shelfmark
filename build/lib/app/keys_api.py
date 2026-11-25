"""Key management endpoints (admin only)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth
from .deps import Conn

router = APIRouter(
    prefix="/api/keys",
    tags=["keys"],
    dependencies=[Depends(auth.require("admin"))],
)


class KeyCreate(BaseModel):
    name: str
    role: str


@router.get("")
def list_keys(conn: Conn) -> dict:
    return {"items": auth.list_keys(conn)}


@router.post("", status_code=201)
def create_key(body: KeyCreate, conn: Conn) -> dict:
    if body.role not in auth.ROLE_ORDER:
        raise HTTPException(status_code=422, detail="invalid role")
    try:
        return auth.create_key(conn, body.name, body.role)
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail="name already exists") from e


@router.post("/{key_id}/revoke", status_code=204)
def revoke_key(key_id: int, conn: Conn) -> None:
    auth.revoke_key(conn, key_id)
