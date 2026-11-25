"""DigiKey pricing: offer ladders, best-offer selection and the price audit.

Money is integer micro-dollars everywhere. The only place a float is tolerated
is the boundary where DigiKey hands us a JSON ``UnitPrice``; it is converted to
micro-dollars immediately with ``Decimal`` and never stored as a float.

The HTTP layer reuses ``app.enrich``'s token, budget and throttle helpers so a
DigiKey call costs one unit of the same daily budget whether it is enrichment
or pricing.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import enrich
from .config import settings
from .mpn import normalise_mpn
from .repo import offers as offers_repo

DIGIKEY_SEARCH_KEYWORD = "/products/v4/search/keyword"


def dollars_to_micros(value: object) -> int:
    """Convert a decimal dollar amount to integer micro-dollars.

    Accepts the float or string forms DigiKey returns. ``str`` of a Python float
    is its shortest decimal representation, so ``Decimal(str(x))`` is exact and
    the conversion never drifts.
    """
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"invalid price: {value!r}") from error
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def format_money(micros: int) -> str:
    """Render integer micro-dollars as ``$12.34``.

    Always shows at least two decimals and trims trailing zeros beyond them, so
    ``23660`` renders ``$0.02366`` and ``11260000`` renders ``$11.26``. Done with
    integer arithmetic so a price never accumulates float error.
    """
    sign = "-" if micros < 0 else ""
    value = abs(int(micros))
    dollars = value // 1_000_000
    frac = value % 1_000_000
    frac_str = f"{frac:06d}"
    decimal = frac_str[:2] + frac_str[2:].rstrip("0")
    return f"{sign}${dollars}.{decimal}"


def _search_keyword_http(mpn: str) -> dict[str, Any]:
    token = enrich._get_access_token()
    url = enrich.DIGIKEY_API_BASE + DIGIKEY_SEARCH_KEYWORD
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": settings.digikey_client_id,
        "Content-Type": "application/json",
    }
    data = json.dumps({"Keywords": mpn, "Limit": 5}).encode()
    try:
        resp = enrich._open(url, 30, headers=headers, data=data)
    except urllib.error.HTTPError as error:
        raise enrich._map_http_error(error) from error
    except urllib.error.URLError as error:
        raise enrich.TransientError(f"network error: {error}") from error
    try:
        return json.load(resp)
    finally:
        resp.close()


def fetch_digikey_offers(mpn: str) -> list[dict[str, Any]]:
    """Fetch every variation ladder for one MPN, enforcing budget and rate."""
    if not enrich._credentials_configured():
        raise enrich.CredentialsError("credentials not configured")
    if not enrich._budget_ok():
        raise enrich.BudgetError("daily DigiKey budget exhausted")
    enrich._throttle()
    enrich._consume_budget()
    return parse_digikey_response(_search_keyword_http(mpn), mpn)


def parse_digikey_response(payload: dict[str, Any], mpn: str) -> list[dict[str, Any]]:
    """Extract one offer per variation from a keyword-search response.

    A keyword search returns near misses, so only products whose
    ``ManufacturerProductNumber`` normalises to *mpn* contribute offers.
    Returns a list of ``{sku, packaging, minimum_quantity, breaks}`` where each
    break is ``{quantity, unit_price_micros}``.
    """
    mpn_norm = normalise_mpn(mpn)
    offers: list[dict[str, Any]] = []
    products = payload.get("Products") or []
    if isinstance(products, dict):
        products = [products]
    for product in products:
        if not isinstance(product, dict):
            continue
        product_mpn = product.get("ManufacturerProductNumber") or ""
        try:
            if normalise_mpn(str(product_mpn)) != mpn_norm:
                continue
        except ValueError:
            continue
        for variation in product.get("ProductVariations") or []:
            if not isinstance(variation, dict):
                continue
            sku = variation.get("DigiKeyProductNumber")
            if not sku:
                continue
            package_type = variation.get("PackageType") or {}
            packaging = package_type.get("Name") if isinstance(package_type, dict) else None
            minimum_quantity = variation.get("MinimumOrderQuantity") or 1
            breaks: list[dict[str, Any]] = []
            for entry in variation.get("StandardPricing") or []:
                if not isinstance(entry, dict):
                    continue
                qty = entry.get("BreakQuantity")
                unit = entry.get("UnitPrice")
                if qty is None or unit is None:
                    continue
                breaks.append({"quantity": int(qty), "unit_price_micros": dollars_to_micros(unit)})
            breaks.sort(key=lambda b: int(b["quantity"]))
            offers.append(
                {
                    "sku": str(sku),
                    "packaging": packaging,
                    "minimum_quantity": int(minimum_quantity),
                    "breaks": breaks,
                }
            )
    return offers


def price_at(offer: dict[str, Any], quantity: int) -> tuple[int, int] | None:
    """Return ``(unit_price_micros, total_micros)`` for *offer* at *quantity*.

    ``None`` when the offer is below its MOQ or has no break at or below the
    requested quantity.
    """
    if quantity < int(offer["minimum_quantity"]):
        return None
    applicable: dict[str, Any] | None = None
    for b in offer["breaks"]:
        if int(b["quantity"]) <= quantity:
            applicable = b
    if applicable is None:
        return None
    unit = int(applicable["unit_price_micros"])
    return unit, quantity * unit


def offers_at(conn: sqlite3.Connection, part_id: int, quantity: int) -> list[dict[str, Any]]:
    """All offers for a part priced at *quantity*, cheapest total first."""
    rows: list[dict[str, Any]] = []
    for offer in offers_repo.list_offers(conn, part_id):
        priced = price_at(offer, quantity)
        row: dict[str, Any] = {
            "offer": offer,
            "unit_price_micros": None,
            "total_micros": None,
            "eligible": False,
        }
        if priced is not None:
            row["unit_price_micros"], row["total_micros"] = priced
            row["eligible"] = True
        rows.append(row)
    rows.sort(key=_offer_rank)
    return rows


def _offer_rank(row: dict[str, Any]) -> tuple[Any, ...]:
    offer = row["offer"]
    fetched = offer.get("fetched_at") or ""
    if row["eligible"]:
        return (0, row["total_micros"], int(offer["minimum_quantity"]), fetched, int(offer["id"]))
    return (1, int(offer["minimum_quantity"]), fetched, int(offer["id"]))


def best_offer(conn: sqlite3.Connection, part_id: int, quantity: int) -> dict[str, Any] | None:
    """The cheapest eligible offer at *quantity*, or ``None``.

    Eligibility requires ``quantity >= minimum_quantity`` and a break at or below
    the requested quantity; the applicable break is the largest one not exceeding
    it. Ties break toward the smaller MOQ, then the earlier-fetched record.
    """
    for row in offers_at(conn, part_id, quantity):
        if row["eligible"]:
            return {
                "offer": row["offer"],
                "unit_price_micros": row["unit_price_micros"],
                "total_micros": row["total_micros"],
            }
    return None


def audit_inversions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Report ladders where buying more costs less in total.

    Each finding is a consecutive ``(q1, p1) -> (q2, p2)`` pair with
    ``q2 * p2 < q1 * p1``, naming the part, distributor and SKU.
    """
    findings: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT o.id, o.sku, d.name AS distributor, p.ipn, p.mpn "
        "FROM offer o "
        "JOIN distributor d ON d.id = o.distributor_id "
        "JOIN part p ON p.id = o.part_id "
        "ORDER BY o.id"
    ).fetchall()
    for row in rows:
        breaks = conn.execute(
            "SELECT quantity, unit_price_micros FROM price_break "
            "WHERE offer_id = ? ORDER BY quantity",
            (row["id"],),
        ).fetchall()
        for i in range(len(breaks) - 1):
            q1 = int(breaks[i]["quantity"])
            p1 = int(breaks[i]["unit_price_micros"])
            q2 = int(breaks[i + 1]["quantity"])
            p2 = int(breaks[i + 1]["unit_price_micros"])
            if q2 * p2 < q1 * p1:
                findings.append(
                    {
                        "ipn": row["ipn"],
                        "mpn": row["mpn"],
                        "distributor": row["distributor"],
                        "sku": row["sku"],
                        "break1": {"quantity": q1, "unit_price_micros": p1},
                        "break2": {"quantity": q2, "unit_price_micros": p2},
                    }
                )
    return findings


def refresh_part_offers(conn: sqlite3.Connection, part: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch a part's DigiKey ladders and replace them transactionally."""
    fetched = fetch_digikey_offers(str(part["mpn"]))
    return offers_repo.upsert_offers(conn, int(part["id"]), "digikey", fetched)
