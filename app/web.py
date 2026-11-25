"""Server-rendered HTML pages (Jinja2, no JS build step)."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .deps import Conn
from .repo import parts as repo
from .repo.parts import DESIGN_STATUSES, DuplicatePartError

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PAGE_SIZE = 50


def _categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM part WHERE category IS NOT NULL ORDER BY category"
    ).fetchall()
    return [row["category"] for row in rows]


def _form_values(form: Any) -> dict[str, str]:
    return {name: str(value) for name, value in form.items()}


@router.get("/")
def index(
    request: Request,
    conn: Conn,
    q: str = "",
    category: str = "",
    page: int = 1,
):
    page = max(1, page)
    items, total = repo.list_parts(
        conn, q=q, category=category or None, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "parts": items,
            "total": total,
            "q": q,
            "category": category,
            "page": page,
            "total_pages": total_pages,
            "categories": _categories(conn),
        },
    )


@router.get("/parts/new")
def new_part(request: Request):
    return templates.TemplateResponse(
        request,
        "new.html",
        {"form": {}, "statuses": DESIGN_STATUSES, "error": None},
    )


@router.post("/parts/new")
async def create_part(request: Request, conn: Conn):
    values = _form_values(await request.form())
    part, error = _create_or_update(conn, values, ipn=None)
    if error is not None:
        return templates.TemplateResponse(
            request,
            "new.html",
            {"form": values, "statuses": DESIGN_STATUSES, "error": error},
        )
    assert part is not None
    return RedirectResponse(f"/parts/{part['ipn']}", status_code=303)


@router.get("/parts/{ipn}")
def part_detail(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"part": part, "form": {}, "statuses": DESIGN_STATUSES, "error": None},
    )


@router.post("/parts/{ipn}")
async def part_update(ipn: str, request: Request, conn: Conn):
    if repo.get_part(conn, ipn) is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    part, error = _create_or_update(conn, values, ipn=ipn)
    if error is not None:
        current = repo.get_part(conn, ipn)
        return templates.TemplateResponse(
            request,
            "detail.html",
            {"part": current, "form": values, "statuses": DESIGN_STATUSES, "error": error},
        )
    assert part is not None
    return RedirectResponse(f"/parts/{part['ipn']}", status_code=303)


def _create_or_update(
    conn: sqlite3.Connection, values: dict[str, str], *, ipn: str | None
) -> tuple[dict | None, str | None]:
    mpn = (values.get("mpn") or "").strip()
    manufacturer = (values.get("manufacturer") or "").strip()
    description = values.get("description") or None
    category = values.get("category") or None
    notes = values.get("notes") or None
    design_status = values.get("design_status") or "active"

    if not mpn:
        return None, "MPN is required."
    if not manufacturer:
        return None, "Manufacturer is required."
    if design_status not in DESIGN_STATUSES:
        return None, "Invalid design status."

    try:
        if ipn is None:
            part: dict | None = repo.create_part(
                conn,
                mpn=mpn,
                manufacturer=manufacturer,
                description=description,
                category=category,
                notes=notes,
                design_status=design_status,
            )
        else:
            part = repo.update_part(
                conn,
                ipn,
                mpn=mpn,
                manufacturer=manufacturer,
                description=description,
                category=category,
                notes=notes,
                design_status=design_status,
            )
    except DuplicatePartError as e:
        return None, f"A part with this MPN and manufacturer already exists ({e.ipn})."
    except ValueError as e:
        return None, str(e)
    if part is None:
        return None, "Part not found."
    return part, None
