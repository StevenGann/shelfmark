"""HTTP API for part substitutions (M4)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator

from . import auth
from .deps import Conn
from .repo import parts as parts_repo
from .repo import substitutes as repo
from .repo.substitutes import DuplicateSubstituteError

router = APIRouter(
    prefix="/api/parts",
    tags=["substitutions"],
    dependencies=[Depends(auth.require("viewer"))],
)

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]

Fit = Literal["exact", "equivalent", "form-fit", "function"]


class SubstituteIn(BaseModel):
    substitute: str
    fit: Fit
    note: str | None = None

    @model_validator(mode="after")
    def _require_note_for_form_fit(self) -> SubstituteIn:
        if self.fit == "form-fit" and not (self.note and self.note.strip()):
            raise ValueError("form-fit requires a note")
        return self


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


@router.get("/{ipn}/substitutes")
def list_substitutes(ipn: str, conn: Conn, direction: str = Query(default="out")) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    if direction == "in":
        items = repo.list_in(conn, int(part["id"]))
    else:
        items = repo.list_out(conn, int(part["id"]))
    return {"items": items, "direction": direction}


@router.post(
    "/{ipn}/substitutes", status_code=201, dependencies=[Depends(auth.require("engineer"))]
)
def add_substitute(ipn: str, body: SubstituteIn, conn: Conn, key: EngineerKey) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    substitute = repo.resolve_part_ref(conn, body.substitute)
    if substitute is None:
        raise HTTPException(status_code=404, detail="unknown substitute part")
    if int(substitute["id"]) == int(part["id"]):
        raise HTTPException(status_code=422, detail="a part cannot substitute for itself")
    try:
        return repo.add_link(
            conn,
            int(part["id"]),
            int(substitute["id"]),
            body.fit,
            body.note,
            _api_actor(key),
        )
    except DuplicateSubstituteError as e:
        raise HTTPException(status_code=409, detail="substitute link already exists") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.delete("/{ipn}/substitutes/{substitute_ipn}")
def remove_substitute(ipn: str, substitute_ipn: str, conn: Conn, key: EngineerKey) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    substitute = parts_repo.get_part(conn, substitute_ipn)
    if substitute is None:
        raise HTTPException(status_code=404, detail="not found")
    if not repo.remove_link(conn, int(part["id"]), int(substitute["id"])):
        raise HTTPException(status_code=404, detail="substitute link not found")
    return {"deleted": True}


@router.get("/{ipn}/substitute-suggestions")
def substitute_suggestions(ipn: str, conn: Conn) -> dict:
    part = parts_repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"items": repo.suggestions(conn, int(part["id"]))}
