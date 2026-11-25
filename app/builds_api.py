"""HTTP API for builds, placements and the OpenPnP exports."""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import auth
from .deps import Conn
from .positions import PositionError, parse_positions
from .repo import boms as boms_repo
from .repo import builds as repo
from .repo.builds import (
    BuildNotFound,
    BuildNotPlanning,
    BuildShortage,
    PositionsRequired,
)

router = APIRouter(tags=["builds"])

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


class BuildCreate(BaseModel):
    boards: int = Field(ge=1)
    note: str | None = None


class CommitIn(BaseModel):
    locations: dict[str, str] | None = None


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _xml_response(content: str, filename: str, extra: dict[str, str] | None = None) -> Response:
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if extra:
        headers.update(extra)
    return Response(content=content, media_type="application/xml", headers=headers)


@router.post("/api/projects/{project_id}/boms/{revision_id}/builds", status_code=201)
def create_build(
    project_id: int, revision_id: int, body: BuildCreate, conn: Conn, key: EngineerKey
) -> dict:
    revision = boms_repo.get_revision(conn, revision_id)
    if revision is None or int(revision["project_id"]) != project_id:
        raise HTTPException(status_code=404, detail="revision not found")
    try:
        return repo.create_build(
            conn,
            revision_id=revision_id,
            boards=body.boards,
            note=body.note,
            created_by=_api_actor(key),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/builds", dependencies=[Depends(auth.require("viewer"))])
def list_builds(conn: Conn) -> dict:
    return {"items": repo.list_builds(conn)}


@router.get("/api/builds/{build_id}", dependencies=[Depends(auth.require("viewer"))])
def get_build(build_id: int, conn: Conn) -> dict:
    build = repo.get_build(conn, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    return build


@router.post("/api/builds/{build_id}/commit")
def commit_build(
    build_id: int,
    conn: Conn,
    key: EngineerKey,
    body: CommitIn | None = None,
) -> dict:
    try:
        return repo.commit_build(conn, build_id, body.locations if body else None, _api_actor(key))
    except BuildNotFound as e:
        raise HTTPException(status_code=404, detail="build not found") from e
    except BuildNotPlanning as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except BuildShortage as e:
        raise HTTPException(status_code=409, detail=e.message) from e


@router.post("/api/builds/{build_id}/abandon")
def abandon_build(build_id: int, conn: Conn, key: EngineerKey) -> dict:
    try:
        return repo.abandon_build(conn, build_id)
    except BuildNotFound as e:
        raise HTTPException(status_code=404, detail="build not found") from e
    except BuildNotPlanning as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/api/revisions/{revision_id}/positions")
async def upload_positions(
    revision_id: int, request: Request, conn: Conn, key: EngineerKey
) -> dict:
    revision = boms_repo.get_revision(conn, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail="file is required")
    data = await upload.read()
    try:
        placements = parse_positions(data)
    except PositionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    result = repo.replace_placements(conn, revision_id, placements)
    return {"sha256": hashlib.sha256(data).hexdigest(), "count": result["count"]}


@router.get(
    "/api/builds/{build_id}/picklist.csv",
    dependencies=[Depends(auth.require("viewer"))],
)
def picklist(build_id: int, conn: Conn) -> Response:
    try:
        return _csv_response(repo.picklist_text(conn, build_id), f"build-{build_id}-picklist.csv")
    except BuildNotFound as e:
        raise HTTPException(status_code=404, detail="build not found") from e


@router.get(
    "/api/builds/{build_id}/board.xml",
    dependencies=[Depends(auth.require("viewer"))],
)
def board_xml(build_id: int, conn: Conn) -> Response:
    try:
        text, skipped = repo.board_xml_text(conn, build_id)
    except BuildNotFound as e:
        raise HTTPException(status_code=404, detail="build not found") from e
    except PositionsRequired as e:
        raise HTTPException(status_code=409, detail="positions_required") from e
    return _xml_response(
        text,
        f"build-{build_id}-board.xml",
        {"X-Skipped-Placements": str(skipped)},
    )


@router.get(
    "/api/builds/{build_id}/job.xml",
    dependencies=[Depends(auth.require("viewer"))],
)
def job_xml(build_id: int, conn: Conn) -> Response:
    try:
        return _xml_response(repo.job_xml_text(conn, build_id), f"build-{build_id}-job.xml")
    except BuildNotFound as e:
        raise HTTPException(status_code=404, detail="build not found") from e
