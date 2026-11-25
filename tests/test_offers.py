"""Tests for M2: offers, price breaks, best-offer selection and the audit."""

from __future__ import annotations

import copy

import pytest

from app import db, pricing
from app.repo import catalog
from app.repo import offers as offers_repo
from app.repo import parts as parts_repo

# The exact variation shape quoted in docs/M2.md, wrapped in the keyword-search
# envelope the live API returns. ``OPA211AIDRG4`` is a deliberate near miss.
SEARCH_PAYLOAD = {
    "Products": [
        {
            "ManufacturerProductNumber": "OPA211AIDRGR",
            "ProductVariations": [
                {
                    "DigiKeyProductNumber": "296-OPA211AIDRGRCT-ND",
                    "PackageType": {"Id": 2, "Name": "Cut Tape (CT)"},
                    "MinimumOrderQuantity": 1,
                    "StandardPricing": [
                        {"BreakQuantity": 1, "UnitPrice": 11.26, "TotalPrice": 11.26},
                        {"BreakQuantity": 10, "UnitPrice": 8.814, "TotalPrice": 88.14},
                        {"BreakQuantity": 25, "UnitPrice": 8.2012, "TotalPrice": 205.03},
                        {"BreakQuantity": 100, "UnitPrice": 7.5285, "TotalPrice": 752.85},
                    ],
                },
                {
                    "DigiKeyProductNumber": "296-OPA211AIDRGRDKR-ND",
                    "PackageType": {"Id": 1, "Name": "Digi-Reel"},
                    "MinimumOrderQuantity": 1,
                    "StandardPricing": [
                        {"BreakQuantity": 1, "UnitPrice": 12.0, "TotalPrice": 12.0},
                        {"BreakQuantity": 10, "UnitPrice": 10.0, "TotalPrice": 100.0},
                    ],
                },
                {
                    "DigiKeyProductNumber": "296-OPA211AIDRGRTR-ND",
                    "PackageType": {"Id": 3, "Name": "Tape & Reel (TR)"},
                    "MinimumOrderQuantity": 3000,
                    "StandardPricing": [
                        {"BreakQuantity": 3000, "UnitPrice": 3.5, "TotalPrice": 10500.0},
                    ],
                },
            ],
        },
        {
            "ManufacturerProductNumber": "OPA211AIDRG4",
            "ProductVariations": [
                {
                    "DigiKeyProductNumber": "296-SOMETHINGELSE-ND",
                    "PackageType": {"Id": 2, "Name": "Cut Tape (CT)"},
                    "MinimumOrderQuantity": 1,
                    "StandardPricing": [
                        {"BreakQuantity": 1, "UnitPrice": 99.99, "TotalPrice": 99.99},
                    ],
                },
            ],
        },
    ]
}


@pytest.fixture
def search_payload():
    return copy.deepcopy(SEARCH_PAYLOAD)


def _create_part(conn, mpn: str = "OPA211AIDRGR"):
    return parts_repo.create_part(conn, mpn=mpn, manufacturer="Texas Instruments")


def _seed_digikey(conn, part_id: int) -> None:
    offers = pricing.parse_digikey_response(SEARCH_PAYLOAD, "OPA211AIDRGR")
    offers_repo.upsert_offers(conn, part_id, "digikey", offers)


def test_parse_real_payload_shape(search_payload):
    offers = pricing.parse_digikey_response(search_payload, "OPA211AIDRGR")
    assert {o["sku"] for o in offers} == {
        "296-OPA211AIDRGRCT-ND",
        "296-OPA211AIDRGRDKR-ND",
        "296-OPA211AIDRGRTR-ND",
    }
    cut_tape = next(o for o in offers if o["sku"] == "296-OPA211AIDRGRCT-ND")
    assert cut_tape["packaging"] == "Cut Tape (CT)"
    assert cut_tape["minimum_quantity"] == 1
    assert cut_tape["breaks"] == [
        {"quantity": 1, "unit_price_micros": 11260000},
        {"quantity": 10, "unit_price_micros": 8814000},
        {"quantity": 25, "unit_price_micros": 8201200},
        {"quantity": 100, "unit_price_micros": 7528500},
    ]
    reel = next(o for o in offers if o["sku"] == "296-OPA211AIDRGRTR-ND")
    assert reel["packaging"] == "Tape & Reel (TR)"
    assert reel["minimum_quantity"] == 3000
    assert reel["breaks"] == [{"quantity": 3000, "unit_price_micros": 3500000}]


def test_keyword_result_with_different_mpn_ignored(search_payload):
    offers = pricing.parse_digikey_response(search_payload, "OPA211AIDRGR")
    assert all(o["sku"] != "296-SOMETHINGELSE-ND" for o in offers)

    only_near_miss = pricing.parse_digikey_response(search_payload, "OPA211AIDRG4")
    assert [o["sku"] for o in only_near_miss] == ["296-SOMETHINGELSE-ND"]


def test_refresh_replaces_ladder_not_appends():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    first = [
        {
            "sku": "CT-1",
            "packaging": "Cut Tape (CT)",
            "minimum_quantity": 1,
            "breaks": [
                {"quantity": 1, "unit_price_micros": 1000000},
                {"quantity": 10, "unit_price_micros": 900000},
            ],
        }
    ]
    offers_repo.upsert_offers(conn, int(part["id"]), "digikey", first)
    second = [
        {
            "sku": "CT-1",
            "packaging": "Cut Tape (CT)",
            "minimum_quantity": 1,
            "breaks": [{"quantity": 1, "unit_price_micros": 500000}],
        }
    ]
    offers_repo.upsert_offers(conn, int(part["id"]), "digikey", second)
    offers = offers_repo.list_offers(conn, int(part["id"]))
    conn.close()

    assert len(offers) == 1
    assert offers[0]["sku"] == "CT-1"
    assert [b["quantity"] for b in offers[0]["breaks"]] == [1]
    assert offers[0]["breaks"][0]["unit_price_micros"] == 500000


def test_refresh_returning_nothing_marks_no_longer_listed():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    _seed_digikey(conn, int(part["id"]))
    before = offers_repo.list_offers(conn, int(part["id"]))
    assert before and all(len(o["breaks"]) > 0 for o in before)

    offers_repo.upsert_offers(conn, int(part["id"]), "digikey", [])
    after = offers_repo.list_offers(conn, int(part["id"]))
    conn.close()

    assert len(after) == len(before)
    assert all(o["breaks"] == [] for o in after)
    assert all(o["source"] == "digikey" for o in after)
    assert all(o["fetched_at"] is not None for o in after)


def test_best_offer_picks_reel_at_5000_and_cut_tape_at_25():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    _seed_digikey(conn, int(part["id"]))
    best_25 = pricing.best_offer(conn, int(part["id"]), 25)
    best_5000 = pricing.best_offer(conn, int(part["id"]), 5000)
    conn.close()

    assert best_25 is not None
    assert best_25["offer"]["sku"] == "296-OPA211AIDRGRCT-ND"
    assert best_25["unit_price_micros"] == 8201200
    assert best_25["total_micros"] == 25 * 8201200

    assert best_5000 is not None
    assert best_5000["offer"]["sku"] == "296-OPA211AIDRGRTR-ND"
    assert best_5000["unit_price_micros"] == 3500000
    assert best_5000["total_micros"] == 5000 * 3500000


def test_offer_below_moq_not_eligible():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    _seed_digikey(conn, int(part["id"]))
    rows = pricing.offers_at(conn, int(part["id"]), 2999)
    best = pricing.best_offer(conn, int(part["id"]), 2999)
    conn.close()

    reel = next(r for r in rows if r["offer"]["sku"] == "296-OPA211AIDRGRTR-ND")
    assert reel["eligible"] is False
    assert best is not None
    assert best["offer"]["sku"] == "296-OPA211AIDRGRCT-ND"


def test_break_boundary_uses_exact_quantity():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    _seed_digikey(conn, int(part["id"]))
    best = pricing.best_offer(conn, int(part["id"]), 10)
    conn.close()

    assert best is not None
    assert best["unit_price_micros"] == 8814000


def test_money_renders_without_float_drift():
    assert pricing.format_money(23660) == "$0.02366"
    assert pricing.format_money(11260000) == "$11.26"
    assert pricing.format_money(8814000) == "$8.814"
    assert pricing.format_money(0) == "$0.00"


def test_audit_flags_inverted_pair_and_stays_quiet_on_normal():
    db.migrate()
    conn = db.connect()
    part = _create_part(conn)
    distributor = catalog.get_distributor_by_name_norm(conn, "digikey")
    assert distributor is not None

    offers_repo.add_manual_offer(
        conn,
        int(part["id"]),
        distributor_id=int(distributor["id"]),
        sku="NORMAL",
        minimum_quantity=1,
        breaks=[
            {"quantity": 1, "unit_price_micros": 11260000},
            {"quantity": 10, "unit_price_micros": 8814000},
            {"quantity": 100, "unit_price_micros": 7528500},
        ],
    )
    assert pricing.audit_inversions(conn) == []

    offers_repo.add_manual_offer(
        conn,
        int(part["id"]),
        distributor_id=int(distributor["id"]),
        sku="INVERTED",
        minimum_quantity=1,
        breaks=[
            {"quantity": 1, "unit_price_micros": 1000000},
            {"quantity": 10, "unit_price_micros": 50000},
        ],
    )
    findings = pricing.audit_inversions(conn)
    conn.close()

    assert len(findings) == 1
    assert findings[0]["sku"] == "INVERTED"
    assert findings[0]["break1"]["quantity"] == 1
    assert findings[0]["break2"]["quantity"] == 10


def test_manual_offer_round_trip_via_api(client):
    part = client.post(
        "/api/parts", json={"mpn": "OPA211AIDRGR", "manufacturer": "Texas Instruments"}
    ).json()
    created = client.post(
        f"/api/parts/{part['ipn']}/offers",
        json={
            "distributor": "Mouser",
            "sku": "595-OPA211AIDRGR",
            "packaging": "Cut Tape (CT)",
            "minimum_quantity": 1,
            "breaks": [
                {"quantity": 1, "unit_price_micros": 11260000},
                {"quantity": 10, "unit_price_micros": 8814000},
            ],
        },
    )
    assert created.status_code == 201, created.text
    offer = created.json()
    assert offer["source"] == "manual"
    assert offer["distributor"] == "Mouser"
    assert [b["quantity"] for b in offer["breaks"]] == [1, 10]

    got = client.get(f"/api/parts/{part['ipn']}/offers", params={"quantity": 10})
    assert got.status_code == 200
    data = got.json()
    assert data["quantity"] == 10
    assert len(data["offers"]) == 1
    assert data["best"] == offer["id"]
    assert data["offers"][0]["unit_price_micros"] == 8814000
    assert data["offers"][0]["total_micros"] == 10 * 8814000


def test_delete_offer_via_api(client):
    part = client.post(
        "/api/parts", json={"mpn": "OPA211AIDRGR", "manufacturer": "Texas Instruments"}
    ).json()
    offer = client.post(
        f"/api/parts/{part['ipn']}/offers",
        json={
            "distributor": "Mouser",
            "sku": "595-OPA211AIDRGR",
            "breaks": [{"quantity": 1, "unit_price_micros": 1000000}],
        },
    ).json()

    r = client.delete(f"/api/offers/{offer['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/parts/{part['ipn']}/offers").json()["offers"] == []


def test_viewer_gets_403_on_writes(client, make_key):
    viewer = make_key("viewer")
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    r = client.post(
        f"/api/parts/{part['ipn']}/offers",
        json={
            "distributor": "Mouser",
            "sku": "SKU-1",
            "breaks": [{"quantity": 1, "unit_price_micros": 1000000}],
        },
        headers={"X-API-Key": viewer},
    )
    assert r.status_code == 403


def test_refresh_endpoint_without_credentials_returns_502(client):
    from app.config import settings

    part = client.post("/api/parts", json={"mpn": "OPA211AIDRGR", "manufacturer": "TI"}).json()
    original_id = settings.digikey_client_id
    original_secret = settings.digikey_client_secret
    settings.digikey_client_id = ""
    settings.digikey_client_secret = ""
    try:
        r = client.post(f"/api/parts/{part['ipn']}/offers/refresh")
    finally:
        settings.digikey_client_id = original_id
        settings.digikey_client_secret = original_secret
    assert r.status_code == 502
