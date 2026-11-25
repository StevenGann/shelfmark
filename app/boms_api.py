"""HTTP API for projects and BOM revisions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import auth
from .bom import BomError, NoMpnColumnError
from .deps import Conn
from .repo import boms as repo
from .repo.boms import DuplicateProjectError, DuplicateRevisionError

router = APIRouter(tags=["boms"])

EngineerKey = Annotated[dict, Depends(auth.require("engineer"))]


def _api_actor(key: dict) -> str:
    return key.get("name") or "api"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None


class ResolveIn(BaseModel):
    ipn: str


@router.get("/api/projects", dependencies=[Depends(auth.require("viewer"))])
def list_projects(conn: Conn) -> dict:
    return {"items": repo.list_projects(conn)}


@router.post("/api/projects", status_code=201)
def create_project(body: ProjectCreate, conn: Conn, key: EngineerKey) -> dict:
    try:
        return repo.create_project(conn, name=body.name, description=body.description)
    except DuplicateProjectError as e:
        raise HTTPException(status_code=409, detail={"project_id": e.project_id}) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/projects/{project_id}", dependencies=[Depends(auth.require("viewer"))])
def get_project(project_id: int, conn: Conn) -> dict:
    project = repo.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    revisions = repo.list_revisions(conn, project_id)
    diff_summary = None
    if revisions:
        newest = revisions[0]
        previous_id = revisions[1]["id"] if len(revisions) > 1 else None
        diff_summary = repo.diff(conn, previous_id, newest["id"])
    return {"project": project, "revisions": revisions, "diff": diff_summary}


@router.post("/api/projects/{project_id}/boms", status_code=201)
async def import_bom(project_id: int, request: Request, conn: Conn, key: EngineerKey) -> dict:
    project = repo.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=422, detail="file is required")
    filename = getattr(upload, "filename", None) or "bom.csv"
    label = str(form.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="label is required")
    note_value = form.get("note")
    note = str(note_value) if note_value else None
    dry_run = _truthy(str(form.get("dry_run") or "")) or _truthy(
        request.query_params.get("dry_run")
    )
    data = await upload.read()

    try:
        return repo.import_bom(
            conn,
            project_id=project_id,
            label=label,
            source_filename=filename,
            data=data,
            imported_by=_api_actor(key),
            note=note,
            dry_run=dry_run,
        )
    except DuplicateRevisionError as e:
        raise HTTPException(status_code=409, detail={"revision_id": e.revision_id}) from e
    except NoMpnColumnError as e:
        raise HTTPException(
            status_code=422, detail={"error": "no MPN column", "headers": e.headers}
        ) from e
    except BomError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/revisions/{revision_id}", dependencies=[Depends(auth.require("viewer"))])
def get_revision(revision_id: int, conn: Conn) -> dict:
    revision = repo.get_revision(conn, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return {"revision": revision, "lines": repo.get_lines(conn, revision_id)}


@router.get("/api/revisions/{revision_id}/diff", dependencies=[Depends(auth.require("viewer"))])
def get_revision_diff(revision_id: int, conn: Conn) -> dict:
    revision = repo.get_revision(conn, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    previous = conn.execute(
        "SELECT id FROM bom_revision WHERE project_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (revision["project_id"], revision_id),
    ).fetchone()
    previous_id = int(previous["id"]) if previous is not None else None
    return repo.diff(conn, previous_id, revision_id)


@router.post("/api/bom-lines/{line_id}/resolve")
def resolve_line(line_id: int, body: ResolveIn, conn: Conn, key: EngineerKey) -> dict:
    try:
        return repo.resolve_line(conn, line_id, body.ipn)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/api/revisions/{revision_id}/resolve-unmatched")
def resolve_unmatched(revision_id: int, conn: Conn, key: EngineerKey) -> dict:
    revision = repo.get_revision(conn, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return {"created": repo.resolve_unmatched(conn, revision_id)}
