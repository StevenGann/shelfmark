"""Server-rendered HTML pages (Jinja2, no JS build step)."""

from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import assets, auth, enrich, labels, pricing, sessions, users
from .bom import BomError
from .config import settings
from .deps import Conn
from .packages import normalise_package
from .ratelimit import allow
from .ratelimit import reset as reset_rate
from .repo import boms as boms_repo
from .repo import catalog
from .repo import offers as offers_repo
from .repo import parts as repo
from .repo import stock as stock_repo
from .repo.boms import DuplicateProjectError, DuplicateRevisionError
from .repo.parts import DESIGN_STATUSES, DuplicatePartError
from .repo.stock import StockError


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_asset_date(value: str | None) -> str:
    """Render a stored ``YYYY-MM-DDTHH:MM:SSZ`` timestamp for the page."""
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %b %Y")
    except ValueError:
        return value


def _fetched_note(value: str | None) -> str:
    """Render the age of the most recent DigiKey offer fetch."""
    if not value:
        return "never fetched"
    try:
        fetched = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return value
    days = (datetime.now(UTC) - fetched).days
    if days <= 0:
        return "fetched just now"
    if days == 1:
        return "fetched 1 day ago"
    return f"fetched {days} days ago"


def _quantity_from(request: Request) -> int:
    """Parse the ``quantity`` query param into a positive integer, else 1."""
    raw = request.query_params.get("quantity", "1")
    try:
        quantity = int(raw)
    except ValueError:
        return 1
    return quantity if quantity >= 1 else 1


router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["package"] = normalise_package
templates.env.filters["assetdate"] = _format_asset_date
templates.env.filters["money"] = pricing.format_money

PAGE_SIZE = 50
SESSION_COOKIE = "smk_session"
LOGIN_ERROR = "Invalid username or password."

_ASSET_KINDS = ("image", "datasheet")
_UPLOAD_CHUNK = 64 * 1024
_WRONG_TYPE_MESSAGE = {
    "datasheet": "Expected a PDF datasheet.",
    "image": "Expected a JPEG, PNG or WebP image.",
}
_TOO_LARGE_MESSAGE = {
    "datasheet": "Datasheet exceeds the 25 MB limit.",
    "image": "Image exceeds the 10 MB limit.",
}


def _categories(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM part WHERE category IS NOT NULL ORDER BY category"
    ).fetchall()
    return [row["category"] for row in rows]


def _packages(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT package FROM part WHERE package IS NOT NULL ORDER BY package"
    ).fetchall()
    return [row["package"] for row in rows]


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
    package: str = "",
    page: int = 1,
    sort: str = "",
):
    page = max(1, page)
    items, total = repo.list_parts(
        conn,
        q=q,
        category=category or None,
        package=package or None,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        sort=sort or None,
    )
    thumbnails = catalog.thumbnails(conn, [int(p["id"]) for p in items])
    for part in items:
        part["image_rel_path"] = thumbnails.get(int(part["id"]))
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    return _render(
        request,
        "list.html",
        {
            "parts": items,
            "total": total,
            "q": q,
            "category": category,
            "package": package,
            "page": page,
            "total_pages": total_pages,
            "sort": sort,
            "categories": _categories(conn),
            "packages": _packages(conn),
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
        _part_detail_context(conn, part, _quantity_from(request)),
    )


@router.post("/parts/{ipn}/offers", dependencies=[Depends(_require_session("engineer"))])
async def add_offer(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    form = await request.form()
    error = _add_manual_offer(conn, part, form, request)
    if error is not None:
        context = _part_detail_context(conn, part)
        context["offer_error"] = error
        return _render(request, "detail.html", context, status_code=400)
    return RedirectResponse(f"/parts/{ipn}#prices", status_code=303)


@router.post("/parts/{ipn}/offers/refresh", dependencies=[Depends(_require_session("engineer"))])
def refresh_offers(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    try:
        pricing.refresh_part_offers(conn, part)
    except (
        enrich.CredentialsError,
        enrich.BudgetError,
        enrich.CatalogueMiss,
        enrich.TransientError,
    ) as error:
        context = _part_detail_context(conn, part, _quantity_from(request))
        context["offer_error"] = str(error)
        return _render(request, "detail.html", context, status_code=502)
    return RedirectResponse(f"/parts/{ipn}#prices", status_code=303)


@router.post("/offers/{offer_id}/delete", dependencies=[Depends(_require_session("engineer"))])
def delete_offer(offer_id: int, conn: Conn):
    offer = offers_repo.get_offer(conn, offer_id)
    if offer is None:
        raise HTTPException(status_code=404, detail="not found")
    part = repo.get_part_by_id(conn, int(offer["part_id"]))
    offers_repo.delete_offer(conn, offer_id)
    if part is None:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/parts/{part['ipn']}#prices", status_code=303)


@router.post("/parts/{ipn}/enrich", dependencies=[Depends(_require_session("engineer"))])
def part_enrich(ipn: str, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    catalog.queue_job(conn, int(part["id"]))
    return RedirectResponse(f"/parts/{ipn}", status_code=303)


def _part_detail_context(
    conn: sqlite3.Connection, part: dict[str, Any], quantity: int = 1
) -> dict[str, Any]:
    links = catalog.list_part_links(conn, int(part["id"]))
    assets = catalog.list_part_assets(conn, int(part["id"]))
    by_kind = {row["kind"]: row for row in assets}
    best = pricing.best_offer(conn, int(part["id"]), quantity)
    offer_rows = pricing.offers_at(conn, int(part["id"]), quantity)
    for row in offer_rows:
        row["is_best"] = best is not None and int(row["offer"]["id"]) == int(best["offer"]["id"])
    fetched_row = conn.execute(
        "SELECT MAX(fetched_at) AS latest FROM offer WHERE part_id = ? AND source = 'digikey'",
        (int(part["id"]),),
    ).fetchone()
    return {
        "part": part,
        "form": {},
        "statuses": DESIGN_STATUSES,
        "error": None,
        "stock_error": None,
        "offer_error": None,
        "stock_total": stock_repo.total_on_hand(conn, int(part["id"])),
        "stock_items": stock_repo.get_stock(conn, int(part["id"])),
        "locations": stock_repo.list_locations(conn),
        "manufacturer_link": next((link for link in links if link["kind"] == "manufacturer"), None),
        "distributor_links": [link for link in links if link["kind"] == "distributor"],
        "datasheet_source": next((link for link in links if link["kind"] == "datasheet"), None),
        "image": by_kind.get("image"),
        "datasheet": by_kind.get("datasheet"),
        "last_job": catalog.latest_job(conn, int(part["id"])),
        "offers": offer_rows,
        "best_offer_id": best["offer"]["id"] if best else None,
        "quantity": quantity,
        "distributors": catalog.list_distributors(conn),
        "fetched_note": _fetched_note(fetched_row["latest"] if fetched_row else None),
    }


@router.post("/parts/{ipn}", dependencies=[Depends(_require_session("engineer"))])
async def part_update(ipn: str, request: Request, conn: Conn):
    if repo.get_part(conn, ipn) is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    part, error = _create_or_update(conn, values, ipn=ipn)
    if error is not None:
        current = repo.get_part(conn, ipn)
        assert current is not None
        context = _part_detail_context(conn, current)
        context.update({"form": values, "error": error})
        return _render(request, "detail.html", context)
    assert part is not None
    return RedirectResponse(f"/parts/{part['ipn']}", status_code=303)


def _stock_error_context(
    request: Request,
    conn: sqlite3.Connection,
    part: dict[str, Any],
    message: str,
    status_code: int = 409,
):
    context = _part_detail_context(conn, part)
    context["stock_error"] = message
    return _render(request, "detail.html", context, status_code=status_code)


def _actor_from_session(request: Request) -> str:
    user = request.state.user
    return user.get("username") or "api"


def _add_manual_offer(
    conn: sqlite3.Connection, part: dict[str, Any], form: Any, request: Request
) -> str | None:
    """Parse the manual-offer form and insert it; return an error message or None."""
    distributor_name = str(form.get("distributor") or "").strip()
    sku = str(form.get("sku") or "").strip()
    packaging = str(form.get("packaging") or "").strip() or None
    url = str(form.get("url") or "").strip() or None
    minimum_quantity = 1
    raw_moq = str(form.get("minimum_quantity") or "").strip()
    if raw_moq:
        try:
            minimum_quantity = int(raw_moq)
        except ValueError:
            return "Minimum quantity must be a whole number."
    if minimum_quantity < 1:
        return "Minimum quantity must be at least 1."
    if not sku:
        return "SKU is required."
    if not distributor_name:
        return "Distributor is required."
    distributor = catalog.get_distributor_by_name_norm(
        conn, catalog.normalise_name(distributor_name)
    )
    if distributor is None:
        return "Unknown distributor."
    breaks: list[dict[str, int]] = []
    for q_raw, p_raw in zip(
        form.getlist("break_quantity"), form.getlist("break_price"), strict=False
    ):
        q = str(q_raw).strip()
        p = str(p_raw).strip()
        if not q:
            continue
        try:
            quantity = int(q)
        except ValueError:
            return "Break quantities must be whole numbers."
        if quantity < 1:
            return "Break quantities must be at least 1."
        if not p:
            return "Each break needs a unit price."
        try:
            micros = pricing.dollars_to_micros(p)
        except ValueError:
            return f"Could not parse unit price: {p}"
        breaks.append({"quantity": quantity, "unit_price_micros": micros})
    actor = _actor_from_session(request)
    try:
        offers_repo.add_manual_offer(
            conn,
            int(part["id"]),
            distributor_id=int(distributor["id"]),
            sku=sku,
            packaging=packaging,
            url=url,
            minimum_quantity=minimum_quantity,
            breaks=breaks,
            created_by=actor,
        )
    except sqlite3.IntegrityError:
        return "An offer with this distributor and SKU already exists."
    except ValueError as error:
        return str(error)
    return None


@router.post("/parts/{ipn}/stock", dependencies=[Depends(_require_session("engineer"))])
async def part_stock_action(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    location = values.get("location") or ""
    kind = values.get("kind") or ""
    note = values.get("note") or None
    try:
        quantity = int(values.get("quantity") or "")
    except ValueError:
        return _stock_error_context(
            request, conn, part, "Quantity must be a whole number.", status_code=400
        )
    try:
        stock_repo.record_move(
            conn,
            int(part["id"]),
            location,
            kind,
            quantity,
            note,
            _actor_from_session(request),
        )
    except StockError as e:
        return _stock_error_context(request, conn, part, str(e))
    except ValueError as e:
        return _stock_error_context(request, conn, part, str(e), status_code=400)
    return RedirectResponse(f"/parts/{ipn}#stock", status_code=303)


@router.post("/parts/{ipn}/stock/move", dependencies=[Depends(_require_session("engineer"))])
async def part_stock_move(ipn: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    from_location = values.get("from_location") or ""
    to_location = values.get("to_location") or ""
    try:
        quantity = int(values.get("quantity") or "")
    except ValueError:
        return _stock_error_context(
            request, conn, part, "Quantity must be a whole number.", status_code=400
        )
    try:
        stock_repo.move_between(
            conn,
            int(part["id"]),
            from_location,
            to_location,
            quantity,
            _actor_from_session(request),
        )
    except StockError as e:
        return _stock_error_context(request, conn, part, str(e))
    except ValueError as e:
        return _stock_error_context(request, conn, part, str(e), status_code=400)
    return RedirectResponse(f"/parts/{ipn}#stock", status_code=303)


def _stock_context(
    conn: sqlite3.Connection,
    *,
    q: str = "",
    location: str = "",
    below: str = "",
    scan: dict[str, Any] | None = None,
    scan_error: str | None = None,
) -> dict[str, Any]:
    """Everything ``stock.html`` needs, including an optional decoded label."""
    below_int: int | None = None
    if below:
        try:
            below_int = int(below)
        except ValueError:
            below_int = None
    rows = stock_repo.list_stock(conn, location=location or None, q=q or None, below=below_int)
    groups: list[dict[str, Any]] = []
    by_norm: dict[str, dict[str, Any]] = {}
    for row in rows:
        norm = row["location_norm"]
        group = by_norm.get(norm)
        if group is None:
            group = {
                "location": row["location"],
                "total": 0,
                "rows": [],
            }
            by_norm[norm] = group
            groups.append(group)
        group["rows"].append(row)
        group["total"] += int(row["quantity"])
    return {
        "groups": groups,
        "locations": stock_repo.list_locations(conn),
        "q": q,
        "location": location,
        "below": below,
        "scan": scan,
        "scan_error": scan_error,
    }


@router.get("/stock", dependencies=[Depends(_require_session())])
def stock_page(
    request: Request,
    conn: Conn,
    q: str = "",
    location: str = "",
    below: str = "",
):
    return _render(
        request,
        "stock.html",
        _stock_context(conn, q=q, location=location, below=below),
    )


def _scan_error_text(exc: labels.LabelError) -> str:
    message = str(exc)
    if exc.payloads:
        message += " Read: " + ", ".join(exc.payloads)
    return message


@router.post("/stock/scan", dependencies=[Depends(_require_session("engineer"))])
async def stock_scan(request: Request, conn: Conn):
    """Decode an uploaded label photo and render the confirmation form.

    The browser has a session, not an API key, so the scan goes through the
    session-authenticated page routes and not through ``/api/labels``. Nothing
    is written here.
    """
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan_error="Choose a label photo first."),
            status_code=400,
        )
    try:
        data = await labels.read_upload(upload)
        _payloads, chosen = labels.decode(data)
    except labels.LabelError as exc:
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan_error=_scan_error_text(exc)),
            status_code=422,
        )
    parsed = labels.parse_label(chosen)
    return _render(
        request,
        "stock.html",
        _stock_context(
            conn,
            scan={"label": parsed, "resolution": labels.resolve_label(conn, parsed)},
        ),
    )


@router.post("/stock/label-receive", dependencies=[Depends(_require_session("engineer"))])
async def stock_label_receive(request: Request, conn: Conn):
    """Confirm a decoded label: create the part if needed, then record the receipt."""
    form = await request.form()
    ipn = str(form.get("ipn") or "").strip()
    location = str(form.get("location") or "").strip()
    raw_quantity = str(form.get("quantity") or "").strip()
    user = getattr(request.state, "user", None)
    actor = str(user["username"]) if user else "web"
    parsed = {
        "mpn": str(form.get("mpn") or "").strip() or None,
        "description": str(form.get("description") or "").strip() or None,
        "digikey_pn": str(form.get("digikey_pn") or "").strip() or None,
        "lot": str(form.get("lot") or "").strip() or None,
        "date_code": str(form.get("date_code") or "").strip() or None,
    }
    scan = {"label": parsed, "resolution": labels.resolve_label(conn, parsed)}
    try:
        quantity = int(raw_quantity)
    except ValueError:
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan=scan, scan_error="Quantity must be a whole number."),
            status_code=422,
        )
    if quantity < 1:
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan=scan, scan_error="Quantity must be at least 1."),
            status_code=422,
        )
    part: dict[str, Any] | None = None
    if ipn:
        part = repo.get_part(conn, ipn)
    else:
        manufacturer = str(form.get("manufacturer") or "").strip()
        if not parsed["mpn"]:
            return _render(
                request,
                "stock.html",
                _stock_context(
                    conn, scan=scan, scan_error="The label has no manufacturer part number."
                ),
                status_code=422,
            )
        if not manufacturer:
            return _render(
                request,
                "stock.html",
                _stock_context(
                    conn, scan=scan, scan_error="Manufacturer is required to create a part."
                ),
                status_code=422,
            )
        try:
            part = labels.create_part_from_label(conn, parsed, manufacturer)
        except DuplicatePartError as exc:
            return _render(
                request,
                "stock.html",
                _stock_context(conn, scan=scan, scan_error=str(exc)),
                status_code=409,
            )
        except ValueError as exc:
            return _render(
                request,
                "stock.html",
                _stock_context(conn, scan=scan, scan_error=str(exc)),
                status_code=422,
            )
    if part is None:
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan=scan, scan_error=f"No part with IPN {ipn}."),
            status_code=404,
        )
    note = (
        "; ".join(
            piece
            for piece in (labels.lot_note(parsed), str(form.get("note") or "").strip() or None)
            if piece
        )
        or None
    )
    try:
        stock_repo.record_move(conn, int(part["id"]), location, "receive", quantity, note, actor)
    except (StockError, ValueError) as exc:
        return _render(
            request,
            "stock.html",
            _stock_context(conn, scan=scan, scan_error=str(exc)),
            status_code=422,
        )
    return RedirectResponse("/stock", status_code=303)


def _project_context(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    error: str | None = None,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = boms_repo.get_project(conn, project_id)
    assert project is not None
    revisions = boms_repo.list_revisions(conn, project_id)
    diff = None
    unmatched_lines: list[dict[str, Any]] = []
    if revisions:
        newest = revisions[0]
        previous_id = revisions[1]["id"] if len(revisions) > 1 else None
        diff = boms_repo.diff(conn, previous_id, newest["id"])
        for line in boms_repo.get_lines(conn, newest["id"]):
            if line["match_status"] != "matched":
                unmatched_lines.append(line)
    return {
        "project": project,
        "revisions": revisions,
        "diff": diff,
        "unmatched_lines": unmatched_lines,
        "error": error,
        "pending": pending,
    }


@router.get("/projects", dependencies=[Depends(_require_session())])
def projects_page(request: Request, conn: Conn):
    return _render(
        request,
        "projects.html",
        {"projects": boms_repo.list_projects(conn), "error": None},
    )


@router.post("/projects", dependencies=[Depends(_require_session("engineer"))])
async def projects_create(request: Request, conn: Conn):
    values = _form_values(await request.form())
    name = (values.get("name") or "").strip()
    error: str | None = None
    if not name:
        error = "Name is required."
    else:
        try:
            boms_repo.create_project(
                conn, name=name, description=(values.get("description") or None)
            )
        except DuplicateProjectError:
            error = f"A project named {name!r} already exists."
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        return _render(
            request,
            "projects.html",
            {"projects": boms_repo.list_projects(conn), "error": error},
            status_code=400,
        )
    return RedirectResponse("/projects", status_code=303)


@router.get("/projects/{project_id}", dependencies=[Depends(_require_session())])
def project_page(project_id: int, request: Request, conn: Conn):
    if boms_repo.get_project(conn, project_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    return _render(request, "project.html", _project_context(conn, project_id))


@router.post("/projects/{project_id}/boms", dependencies=[Depends(_require_session("engineer"))])
async def project_import(project_id: int, request: Request, conn: Conn):
    project = boms_repo.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="not found")
    form = await request.form()
    user = request.state.user
    actor = user.get("username") or "api"

    if (form.get("_confirm") or "") == "1":
        sha256 = str(form.get("sha256") or "")
        label = str(form.get("label") or "").strip()
        note = str(form.get("note") or "") or None
        filename = str(form.get("filename") or "") or "bom.csv"
        rel_path = boms_repo.bom_rel_path(str(project["name"]), sha256)
        path = assets.resolve(rel_path)
        if path is None or not path.is_file():
            return _render(
                request,
                "project.html",
                _project_context(
                    conn, project_id, error="The uploaded file was not found; please re-upload."
                ),
                status_code=400,
            )
        try:
            boms_repo.import_bom(
                conn,
                project_id=project_id,
                label=label,
                source_filename=filename,
                data=path.read_bytes(),
                imported_by=actor,
                note=note,
            )
        except DuplicateRevisionError as exc:
            return _render(
                request,
                "project.html",
                _project_context(
                    conn,
                    project_id,
                    error=f"This file was already imported as revision {exc.revision_id}.",
                ),
                status_code=409,
            )
        except (BomError, ValueError) as exc:
            return _render(
                request,
                "project.html",
                _project_context(conn, project_id, error=str(exc)),
                status_code=400,
            )
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    upload = form.get("file")
    label = str(form.get("label") or "").strip()
    note = str(form.get("note") or "") or None
    if upload is None or not hasattr(upload, "read"):
        return _render(
            request,
            "project.html",
            _project_context(conn, project_id, error="No file was selected."),
            status_code=400,
        )
    filename = getattr(upload, "filename", None) or "bom.csv"
    data = await upload.read()
    try:
        result = boms_repo.import_bom(
            conn,
            project_id=project_id,
            label=label or "import",
            source_filename=filename,
            data=data,
            imported_by=actor,
            note=note,
            dry_run=True,
        )
    except (BomError, ValueError) as exc:
        return _render(
            request,
            "project.html",
            _project_context(conn, project_id, error=str(exc)),
            status_code=400,
        )

    sha256 = hashlib.sha256(data).hexdigest()
    rel_path = boms_repo.bom_rel_path(str(project["name"]), sha256)
    tmp = assets.tmp_path(rel_path)
    tmp.write_bytes(data)
    assets.commit(tmp, rel_path)

    pending = {
        "label": label,
        "note": note or "",
        "filename": filename,
        "sha256": sha256,
        "diff": result,
    }
    return _render(request, "project.html", _project_context(conn, project_id, pending=pending))


@router.get("/revisions/{revision_id}", dependencies=[Depends(_require_session())])
def revision_page(revision_id: int, request: Request, conn: Conn):
    revision = boms_repo.get_revision(conn, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="not found")
    project = boms_repo.get_project(conn, revision["project_id"])
    return _render(
        request,
        "revision.html",
        {
            "revision": revision,
            "project": project,
            "lines": boms_repo.get_lines(conn, revision_id),
            "error": None,
        },
    )


@router.post("/bom-lines/{line_id}/resolve", dependencies=[Depends(_require_session("engineer"))])
async def line_resolve(line_id: int, request: Request, conn: Conn):
    line = boms_repo.get_line(conn, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="not found")
    values = _form_values(await request.form())
    ipn = (values.get("ipn") or "").strip()
    return_to = _safe_next(values.get("return_to") or f"/revisions/{line['revision_id']}")
    try:
        boms_repo.resolve_line(conn, line_id, ipn)
    except ValueError as exc:
        revision = boms_repo.get_revision(conn, line["revision_id"])
        assert revision is not None
        return _render(
            request,
            "revision.html",
            {
                "revision": revision,
                "project": boms_repo.get_project(conn, revision["project_id"]),
                "lines": boms_repo.get_lines(conn, line["revision_id"]),
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(return_to, status_code=303)


@router.post(
    "/revisions/{revision_id}/resolve-unmatched",
    dependencies=[Depends(_require_session("engineer"))],
)
def revision_resolve_unmatched(revision_id: int, conn: Conn):
    if boms_repo.get_revision(conn, revision_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    boms_repo.resolve_unmatched(conn, revision_id)
    return RedirectResponse(f"/revisions/{revision_id}", status_code=303)


@router.get("/assets/{rel_path:path}", dependencies=[Depends(_require_session())])
def serve_asset(rel_path: str, conn: Conn):
    row = catalog.get_part_asset_by_rel_path(conn, rel_path)
    if row is None or row["status"] != "ok":
        raise HTTPException(status_code=404, detail="not found")
    path = assets.resolve(rel_path)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    headers = {"Cache-Control": "private, max-age=86400"}
    if row["content_type"] == "application/pdf":
        headers["Content-Disposition"] = "inline"
    return FileResponse(path, media_type=row["content_type"], headers=headers)


def _remove_file(rel_path: str) -> None:
    path = assets.resolve(rel_path)
    if path is not None:
        path.unlink(missing_ok=True)


def _remove_asset(conn: sqlite3.Connection, part_id: int, kind: str) -> None:
    """Drop the recorded rows of *kind* and their files on disk."""
    for row in catalog.list_part_assets(conn, part_id):
        if row["kind"] == kind:
            _remove_file(row["rel_path"])
    catalog.delete_part_assets_of_kind(conn, part_id, kind)


async def _store_upload(ipn: str, kind: str, upload: Any) -> tuple[Path, str, str, str, str, int]:
    """Stream *upload* to a temp file, validating content and size.

    Returns ``(tmp, rel_path, content_type, sha256, filename, size)``. The temp
    file is removed on every failure path; the caller commits it into place.
    """
    if upload is None or not hasattr(upload, "read"):
        raise ValueError("No file was selected.")
    max_bytes = assets.DATASHEET_MAX_BYTES if kind == "datasheet" else assets.IMAGE_MAX_BYTES
    filename = getattr(upload, "filename", None) or ""
    hasher = hashlib.sha256()
    total = 0
    rel_path = ""
    content_type = ""
    tmp: Path | None = None
    out = None
    try:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            if out is None:
                sniffed = assets.content_type_for(kind, chunk)
                if sniffed is None:
                    raise ValueError(_WRONG_TYPE_MESSAGE[kind])
                content_type, ext = sniffed
                rel_path = assets.rel_path_for(ipn, kind, ext)
                tmp = assets.tmp_path(rel_path)
                out = open(tmp, "wb")
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(_TOO_LARGE_MESSAGE[kind])
            hasher.update(chunk)
            out.write(chunk)
        if out is None:
            raise ValueError("The uploaded file is empty.")
        out.close()
        out = None
        assert tmp is not None
        return tmp, rel_path, content_type, hasher.hexdigest(), filename, total
    except BaseException:
        if out is not None:
            out.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        raise


@router.post("/parts/{ipn}/assets/{kind}", dependencies=[Depends(_require_session("engineer"))])
async def upload_asset(ipn: str, kind: str, request: Request, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    if kind not in _ASSET_KINDS:
        raise HTTPException(status_code=404, detail="not found")

    user = request.state.user
    form = await request.form()
    if (form.get("_method") or "") == "delete":
        _remove_asset(conn, int(part["id"]), kind)
        return RedirectResponse(f"/parts/{ipn}", status_code=303)

    try:
        tmp, rel_path, content_type, sha256, filename, size = await _store_upload(
            ipn, kind, form.get("file")
        )
    except ValueError as error:
        context = _part_detail_context(conn, part)
        context["error"] = str(error)
        return _render(request, "detail.html", context, status_code=400)

    # Commit the new file first: if the write fails, the asset that was already
    # there survives. Old files that lived at a different path go afterwards.
    try:
        assets.commit(tmp, rel_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    for row in catalog.list_part_assets(conn, int(part["id"])):
        if row["kind"] == kind and row["rel_path"] != rel_path:
            _remove_file(row["rel_path"])
    catalog.delete_part_assets_of_kind(conn, int(part["id"]), kind)

    catalog.add_part_asset(
        conn,
        int(part["id"]),
        kind=kind,
        source_url=f"upload:{user['username']}:{sha256[:12]}",
        rel_path=rel_path,
        content_type=content_type,
        bytes=size,
        sha256=sha256,
        fetched_at=_utc_now(),
        status="ok",
        source="upload",
        uploaded_by=user["username"],
        original_filename=filename,
    )
    return RedirectResponse(f"/parts/{ipn}", status_code=303)


@router.delete("/parts/{ipn}/assets/{kind}", dependencies=[Depends(_require_session("engineer"))])
def delete_asset(ipn: str, kind: str, conn: Conn):
    part = repo.get_part(conn, ipn)
    if part is None:
        raise HTTPException(status_code=404, detail="not found")
    if kind not in _ASSET_KINDS:
        raise HTTPException(status_code=404, detail="not found")
    _remove_asset(conn, int(part["id"]), kind)
    return RedirectResponse(f"/parts/{ipn}", status_code=303)


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
