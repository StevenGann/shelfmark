"""Server-rendered HTML pages (Jinja2, no JS build step)."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth, sessions, users
from .config import settings
from .deps import Conn
from .ratelimit import allow
from .ratelimit import reset as reset_rate
from .repo import parts as repo
from .repo.parts import DESIGN_STATUSES, DuplicatePartError

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PAGE_SIZE = 50
SESSION_COOKIE = "smk_session"
LOGIN_ERROR = "Invalid username or password."


def _categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM part WHERE category IS NOT NULL ORDER BY category"
    ).fetchall()
    return [row["category"] for row in rows]


def _form_values(form: Any) -> dict[str, str]:
    return {name: str(value) for name, value in form.items()}


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    context = dict(context)
    context.setdefault("current_user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def _safe_next(value: str) -> str:
    """Only a local absolute path is acceptable as a post-login redirect.

    ``//host`` is protocol-relative and a backslash is treated as a slash by
    several browsers, so both are refused rather than normalised.
    """
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 for ch in value):
        return "/"
    return value


def _set_session_cookie(response: RedirectResponse, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=sessions.SESSION_MAX_AGE,
    )


def _require_session(role: str = "viewer"):
    """FastAPI dependency: resolve the session cookie to a live user.

    Redirects a browser without a valid session to ``/setup`` when no users exist
    yet, otherwise to ``/login``. Raises 403 when the user's role is too low.
    """
    if role not in auth.ROLE_ORDER:
        raise ValueError(f"unknown role: {role}")

    def dependency(request: Request, conn: Conn) -> dict[str, Any]:
        if settings.auth == "none":
            request.state.user = {}
            return {}
        if users.count_users(conn) == 0:
            raise HTTPException(status_code=302, headers={"Location": "/setup"})
        token = request.cookies.get(SESSION_COOKIE)
        user = sessions.resolve(conn, token) if token else None
        if user is None:
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            raise HTTPException(
                status_code=302,
                headers={"Location": "/login?next=" + quote(target, safe="")},
            )
        if auth.ROLE_ORDER[user["role"]] < auth.ROLE_ORDER[role]:
            raise HTTPException(status_code=403, detail="insufficient role")
        sessions.touch(conn, int(user["session_id"]))
        request.state.user = user
        return user

    return dependency


def _enabled_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM app_user WHERE role = 'admin' AND disabled_at IS NULL"
    ).fetchone()
    return int(row["n"])


def _role_change_error(
    conn: sqlite3.Connection, actor: dict[str, Any], target: dict[str, Any] | None, role: str
) -> str | None:
    if target is None:
        return "User not found."
    if role not in auth.ROLE_ORDER:
        return "Invalid role."
    if int(target["id"]) == int(actor["id"]):
        return "You cannot change your own role."
    if target["role"] == "admin" and target["disabled_at"] is None and role != "admin":
        if _enabled_admin_count(conn) <= 1:
            return "The last enabled admin cannot be demoted."
    return None


def _disable_error(
    conn: sqlite3.Connection, actor: dict[str, Any], target: dict[str, Any] | None
) -> str | None:
    if target is None:
        return "User not found."
    if int(target["id"]) == int(actor["id"]):
        return "You cannot disable yourself."
    if target["role"] == "admin" and target["disabled_at"] is None:
        if _enabled_admin_count(conn) <= 1:
            return "The last enabled admin cannot be disabled."
    return None


@router.get("/setup")
def setup_form(request: Request, conn: Conn):
    if users.count_users(conn) != 0:
        raise HTTPException(status_code=404, detail="not found")
    return _render(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup(request: Request, conn: Conn):
    if users.count_users(conn) != 0:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    confirm = values.get("password_confirm") or ""
    error: str | None = None
    if password != confirm:
        error = "Passwords do not match."
    else:
        try:
            users.create_user(conn, username, password, "admin")
        except ValueError as e:
            error = str(e)
    if error is not None:
        return _render(request, "setup.html", {"error": error}, status_code=400)
    return RedirectResponse("/login", status_code=303)


@router.get("/login")
def login_form(request: Request, conn: Conn, next: str = ""):
    if users.count_users(conn) == 0:
        return RedirectResponse("/setup", status_code=303)
    return _render(request, "login.html", {"next": _safe_next(next), "error": None})


@router.post("/login")
async def login(request: Request, conn: Conn):
    values = _form_values(await request.form())
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    next_url = _safe_next(values.get("next") or "/")
    if users.count_users(conn) == 0:
        return RedirectResponse("/setup", status_code=303)

    ip = request.client.host if request.client else "unknown"
    bucket = f"login:{ip}:{username.lower()}"
    wait = allow(bucket, 5, window=900)
    if wait:
        limited = _render(
            request,
            "login.html",
            {"next": next_url, "error": "Too many attempts. Try again shortly."},
            status_code=429,
        )
        limited.headers["Retry-After"] = str(wait)
        return limited

    user = users.get_user_by_username(conn, username)
    if (
        user is None
        or user["disabled_at"] is not None
        or not users.verify_password(user["password_hash"], password)
    ):
        return _render(
            request, "login.html", {"next": next_url, "error": LOGIN_ERROR}, status_code=401
        )
    # A correct password clears the failure count for this (ip, username).
    reset_rate(bucket)
    if users.needs_rehash(user["password_hash"]):
        users.set_password(conn, int(user["id"]), password)
    users.stamp_login(conn, int(user["id"]))
    token = sessions.start(conn, int(user["id"]))
    response = RedirectResponse(next_url, status_code=303)
    _set_session_cookie(response, request, token)
    return response


@router.post("/logout")
def logout(request: Request, conn: Conn):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        sessions.end(conn, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/account", dependencies=[Depends(_require_session())])
def account_form(request: Request):
    return _render(request, "account.html", {"error": None, "ok": False})


@router.post("/account", dependencies=[Depends(_require_session())])
async def account_change(request: Request, conn: Conn):
    user = request.state.user
    values = _form_values(await request.form())
    current = values.get("current_password") or ""
    new = values.get("new_password") or ""
    confirm = values.get("new_password_confirm") or ""
    if not users.verify_password(user["password_hash"], current):
        return _render(
            request,
            "account.html",
            {"error": "Current password is incorrect.", "ok": False},
            status_code=401,
        )
    error: str | None = None
    if new != confirm:
        error = "Passwords do not match."
    else:
        try:
            users.validate_password(new, user["username"])
        except ValueError as e:
            error = str(e)
    if error is not None:
        return _render(request, "account.html", {"error": error, "ok": False}, status_code=400)
    users.set_password(conn, int(user["id"]), new)
    sessions.end_all_for_user(conn, int(user["id"]), except_id=int(user["session_id"]))
    return _render(request, "account.html", {"error": None, "ok": True})


@router.get("/users", dependencies=[Depends(_require_session("admin"))])
def users_page(request: Request, conn: Conn):
    return _render(request, "users.html", {"users": users.list_users(conn), "roles": auth.ROLES})


@router.post("/users", dependencies=[Depends(_require_session("admin"))])
async def users_create(request: Request, conn: Conn):
    values = _form_values(await request.form())
    username = (values.get("username") or "").strip()
    password = values.get("password") or ""
    role = values.get("role") or "viewer"
    error: str | None = None
    if role not in auth.ROLE_ORDER:
        error = "Invalid role."
    else:
        try:
            users.create_user(conn, username, password, role)
        except ValueError as e:
            error = str(e)
        except sqlite3.IntegrityError:
            error = "That username is already taken."
    if error is not None:
        return _render(
            request,
            "users.html",
            {"users": users.list_users(conn), "roles": auth.ROLES, "error": error},
            status_code=400,
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role", dependencies=[Depends(_require_session("admin"))])
async def users_set_role(user_id: int, request: Request, conn: Conn):
    actor = request.state.user
    target = users.get_user_by_id(conn, user_id)
    role = (_form_values(await request.form())).get("role") or ""
    error = _role_change_error(conn, actor, target, role)
    if error is not None:
        return _render(
            request,
            "users.html",
            {"users": users.list_users(conn), "roles": auth.ROLES, "error": error},
            status_code=400,
        )
    users.set_role(conn, user_id, role)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/disable", dependencies=[Depends(_require_session("admin"))])
async def users_disable(user_id: int, request: Request, conn: Conn):
    actor = request.state.user
    target = users.get_user_by_id(conn, user_id)
    error = _disable_error(conn, actor, target)
    if error is not None:
        return _render(
            request,
            "users.html",
            {"users": users.list_users(conn), "roles": auth.ROLES, "error": error},
            status_code=400,
        )
    users.set_disabled(conn, user_id, True)
    sessions.end_all_for_user(conn, user_id)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/enable", dependencies=[Depends(_require_session("admin"))])
def users_enable(user_id: int, conn: Conn):
    users.set_disabled(conn, user_id, False)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(_require_session("admin"))])
async def users_reset_password(user_id: int, request: Request, conn: Conn):
    values = _form_values(await request.form())
    password = values.get("password") or ""
    target = users.get_user_by_id(conn, user_id)
    error: str | None = None
    if target is None:
        error = "User not found."
    else:
        try:
            users.validate_password(password, target["username"])
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        return _render(
            request,
            "users.html",
            {"users": users.list_users(conn), "roles": auth.ROLES, "error": error},
            status_code=400,
        )
    users.set_password(conn, user_id, password)
    sessions.end_all_for_user(conn, user_id)
    return RedirectResponse("/users", status_code=303)


@router.get("/", dependencies=[Depends(_require_session())])
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
    return _render(
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


@router.get("/parts/new", dependencies=[Depends(_require_session())])
def new_part(request: Request):
    return _render(
        request,
        "new.html",
        {"form": {}, "statuses": DESIGN_STATUSES, "error": None},
    )


@router.post("/parts/new", dependencies=[Depends(_require_session("engineer"))])
async def create_part(request: Request, conn: Conn):
    values = _form_values(await request.form())
    part, error = _create_or_update(conn, values, ipn=None)
    if error is not None:
        return _render(
            request,
            "new.html",
            {"form": values, "statuses": DESIGN_STATUSES, "error": error},
        )
    assert part is not None
    return RedirectResponse(f"/parts/{part['ipn']}", status_code=303)


@router.get("/parts/{ipn}", dependencies=[Depends(_require_session())])
def part_detail(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    return _render(
        request,
        "detail.html",
        {"part": part, "form": {}, "statuses": DESIGN_STATUSES, "error": None},
    )


@router.post("/parts/{ipn}", dependencies=[Depends(_require_session("engineer"))])
async def part_update(ipn: str, request: Request, conn: Conn):
    if repo.get_part(conn, ipn) is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    part, error = _create_or_update(conn, values, ipn=ipn)
    if error is not None:
        current = repo.get_part(conn, ipn)
        return _render(
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
