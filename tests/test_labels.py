import io

import zxingcpp
from PIL import Image

from app import db, labels
from app.repo import catalog
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo

MH10_PAYLOAD = (
    "[)>\x1e06\x1dP296-OPA211AIDRGRCT-ND\x1d1POPA211AIDRGR"
    "\x1d30P296-OPA211AIDRGRCT-ND\x1dKCAN AND THERMOMETER"
    "\x1d1K99932782\x1d10K127846989\x1d9D2524+5\x1d1T5397196ML5"
    "\x1d11K1\x1d4LDE\x1dQ25\x1d11ZPICK\x1d12Z26564848"
    "\x1d13Z999999\x1d20Z0000"
)


def _png(payload: str, fmt: zxingcpp.BarcodeFormat) -> bytes:
    barcode = zxingcpp.create_barcode(payload, fmt)
    image = barcode.to_image()
    height, width = image.shape
    pil = Image.frombuffer("L", (width, height), memoryview(image).tobytes(), "raw", "L", 0, 1)
    buf = io.BytesIO()
    pil.save(buf, "PNG")
    return buf.getvalue()


def _make_part(mpn="OPA211AIDRGR", manufacturer="Texas Instruments"):
    conn = db.connect()
    try:
        return parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
    finally:
        conn.close()


def _make_part_with_digikey_link(mpn, manufacturer, sku):
    conn = db.connect()
    try:
        part = parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
        digikey = catalog.get_distributor_by_name_norm(conn, "digikey")
        catalog.add_part_link(
            conn,
            int(part["id"]),
            kind="distributor",
            label="DigiKey",
            url=f"https://www.digikey.com/en/products/result?keywords={sku}",
            distributor_id=int(digikey["id"]) if digikey else None,
            sku=sku,
            source="test",
        )
        return part
    finally:
        conn.close()


def test_full_mh10_datamatrix_parses_to_every_field():
    _payloads, chosen = labels.decode(_png(MH10_PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix))
    parsed = labels.parse_label(chosen)
    assert parsed["distributor_pn"] == "296-OPA211AIDRGRCT-ND"
    assert parsed["mpn"] == "OPA211AIDRGR"
    assert parsed["description"] == "CAN AND THERMOMETER"
    assert parsed["quantity"] == 25
    assert parsed["lot"] == "5397196ML5"
    assert parsed["date_code"] == "2524+5"
    assert parsed["country"] == "DE"
    assert parsed["purchase_order"] == "127846989"
    assert parsed["raw"] == MH10_PAYLOAD
    assert parsed["fields"]["11Z"] == "PICK"
    assert parsed["fields"]["12Z"] == "26564848"


def test_code128_digikey_pn_resolves_by_part_number():
    pn = "296-OPA211AIDRGRCT-ND"
    part = _make_part_with_digikey_link("OPA211AIDRGR", "Texas Instruments", pn)
    _payloads, chosen = labels.decode(_png(pn, zxingcpp.BarcodeFormat.Code128))
    parsed = labels.parse_label(chosen)
    assert parsed["distributor_pn"] == pn

    conn = db.connect()
    try:
        resolution = labels.resolve_label(conn, parsed)
        assert resolution["status"] == "matched"
        assert resolution["matched_by"] == "distributor_pn"
        assert resolution["part"]["ipn"] == part["ipn"]
    finally:
        conn.close()


def test_image_with_no_usable_barcode_returns_422_and_lists_payloads(client):
    r = client.post(
        "/api/labels/decode",
        files={
            "file": ("label.png", _png("019514918", zxingcpp.BarcodeFormat.Code128), "image/png")
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["payloads"] == ["019514918"]


def test_image_with_no_barcode_returns_422(client):
    buf = io.BytesIO()
    Image.new("L", (200, 200), 255).save(buf, "PNG")
    r = client.post(
        "/api/labels/decode", files={"file": ("blank.png", buf.getvalue(), "image/png")}
    )
    assert r.status_code == 422
    assert "message" in r.json()["detail"]


def test_known_mpn_resolves_when_pn_absent():
    part = _make_part()
    conn = db.connect()
    try:
        resolution = labels.resolve_label(conn, {"distributor_pn": None, "mpn": part["mpn"]})
        assert resolution["status"] == "matched"
        assert resolution["matched_by"] == "mpn"
        assert resolution["part"]["ipn"] == part["ipn"]
    finally:
        conn.close()


def test_decode_unknown_label_offers_creation(client):
    r = client.post(
        "/api/labels/decode",
        files={
            "file": (
                "label.png",
                _png(MH10_PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix),
                "image/png",
            )
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"]["status"] == "unknown"
    assert body["label"]["mpn"] == "OPA211AIDRGR"


def test_receive_unknown_label_creates_part_with_sku_link(client):
    r = client.post(
        "/api/labels/receive",
        files={
            "file": (
                "label.png",
                _png(MH10_PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix),
                "image/png",
            )
        },
        data={"location": "Drawer A", "create_part": "true", "manufacturer": "Texas Instruments"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["part"]["mpn"] == "OPA211AIDRGR"
    assert body["part"]["manufacturer"] == "Texas Instruments"

    conn = db.connect()
    try:
        links = catalog.list_part_links(conn, int(body["part"]["id"]))
        skus = [link["sku"] for link in links if link["kind"] == "distributor"]
        assert "296-OPA211AIDRGRCT-ND" in skus
    finally:
        conn.close()


def test_receive_writes_move_with_lot_and_date_code(client):
    pn = "296-OPA211AIDRGRCT-ND"
    part = _make_part_with_digikey_link("OPA211AIDRGR", "Texas Instruments", pn)
    r = client.post(
        "/api/labels/receive",
        files={
            "file": (
                "label.png",
                _png(MH10_PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix),
                "image/png",
            )
        },
        data={"location": "Drawer A"},
    )
    assert r.status_code == 201

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT sm.note FROM stock_move sm "
            "JOIN stock_item si ON si.id = sm.stock_item_id "
            "WHERE si.part_id = ? ORDER BY sm.id",
            (int(part["id"]),),
        ).fetchone()
        assert row["note"] == "lot 5397196ML5, DC 2524+5"
    finally:
        conn.close()


def test_suggested_location_is_last_receive_location():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        stock_repo.record_move(conn, pid, "Drawer A", "receive", 5, None, "tester")
        stock_repo.record_move(conn, pid, "Drawer B", "receive", 3, None, "tester")
        resolution = labels.resolve_label(conn, {"distributor_pn": None, "mpn": part["mpn"]})
        assert resolution["suggested_location"] == "Drawer B"
    finally:
        conn.close()


def test_viewer_gets_403_on_both_endpoints(anon_client, make_key):
    key = make_key("viewer")
    png = _png(MH10_PAYLOAD, zxingcpp.BarcodeFormat.DataMatrix)
    for path, data in (
        ("/api/labels/decode", None),
        ("/api/labels/receive", {"location": "Drawer A"}),
    ):
        r = anon_client.post(
            path,
            files={"file": ("label.png", png, "image/png")},
            data=data,
            headers={"X-API-Key": key},
        )
        assert r.status_code == 403


MOUSER_TMP118 = (
    '[)>\x1e06\x1dKQUAD ADC, THERM,R"\x1d14K003\x1d1PTMP118MBIYMSR\x1dQ20'
    "\x1d11K091050594\x1d4LCN\x1d1VTI\x1e\x04"
)

# The KOA bag: the Data Matrix has no 1P at all, so the MPN only exists in the
# linear code as Mouser's "<3-digit mfr id>-<mpn>".
MOUSER_KOA = (
    "[)>\x1e06\x1dKWO9593\x1d14K020\x1dP6-8,11,13-4,41-4,51-2\x1dQ160"
    "\x1d11K087961310\x1d4LJP\x1d1VKOA Speer\x1e\x04"
)


def test_mouser_label_carries_the_manufacturer_and_quantity():
    parsed = labels.parse_label(MOUSER_TMP118)

    assert parsed["mpn"] == "TMP118MBIYMSR"
    assert parsed["manufacturer"] == "TI"
    assert parsed["quantity"] == 20
    assert parsed["country"] == "CN"
    assert parsed["description"] == 'QUAD ADC, THERM,R"'


def test_mouser_label_without_an_mpn_field_uses_the_linear_code():
    parsed = labels.parse_label(MOUSER_KOA, ["020", "660-RN73H1ETP1001F25", "087961310"])

    assert parsed["mpn"] == "RN73H1ETP1001F25"
    assert parsed["distributor_pn"] == "660-RN73H1ETP1001F25"
    assert parsed["quantity"] == 160
    assert parsed["manufacturer"] == "KOA Speer"


def test_a_digikey_part_number_is_not_mistaken_for_a_mouser_one():
    """Both look like "<digits>-<part>"; only DigiKey adds a packaging suffix."""
    parsed = labels.parse_label(MOUSER_KOA, ["296-OPA211AIDRGRCT-ND"])

    assert parsed["mpn"] is None
    assert parsed["distributor_pn"] is None


def test_a_mouser_sku_resolves_a_part_by_its_supplier_number():
    from app import db
    from app.repo import catalog
    from app.repo import parts as parts_repo

    conn = db.connect()
    try:
        part = parts_repo.create_part(conn, mpn="RN73H1ETP1001F25", manufacturer="KOA Speer")
        mouser = catalog.get_distributor_by_name_norm(conn, "mouser")
        assert mouser is not None
        catalog.add_part_link(
            conn,
            int(part["id"]),
            kind="distributor",
            label="Mouser",
            url="https://www.mouser.com/c/?q=660-RN73H1ETP1001F25",
            distributor_id=int(mouser["id"]),
            sku="660-RN73H1ETP1001F25",
            source="label",
            fetched_at=None,
        )
        parsed = labels.parse_label(MOUSER_KOA, ["660-RN73H1ETP1001F25"])
        resolution = labels.resolve_label(conn, parsed)
    finally:
        conn.close()

    assert resolution["matched_by"] == "distributor_pn"
    assert resolution["part"]["ipn"] == part["ipn"]


def test_a_label_with_no_readable_2d_code_still_prefills_from_the_linear_codes():
    """The TMP118 bag in the house: the Data Matrix did not read, the bars did."""
    payloads = [
        "091050594",
        "20",
        "TMP118MBIYMSR",
        "595-TMP118MBIYMSR",
        "003",
        "CN",
        "54FNC4",
    ]
    parsed = labels.parse_label("", payloads)

    assert parsed["distributor_pn"] == "595-TMP118MBIYMSR"
    assert parsed["mpn"] == "TMP118MBIYMSR"
    assert parsed["quantity"] == 20
    assert parsed["country"] == "CN"


def test_a_mouser_bag_stores_its_sku_under_mouser():
    """The distributor link has to name the right supplier, or the URL is wrong."""
    from app import db

    conn = db.connect()
    try:
        parsed = labels.parse_label(MOUSER_KOA, ["660-RN73H1ETP1001F25"])
        assert parsed["supplier"] == "Mouser"
        part = labels.create_part_from_label(conn, parsed, "KOA Speer")
        row = conn.execute(
            "SELECT d.name AS name FROM part_link pl "
            "LEFT JOIN distributor d ON d.id = pl.distributor_id WHERE pl.part_id = ?",
            (int(part["id"]),),
        ).fetchone()
    finally:
        conn.close()

    assert row["name"] == "Mouser"


def test_a_digikey_label_is_attributed_to_digikey():
    payload = (
        "[)>\x1e06\x1dP296-OPA211AIDRGRCT-ND\x1d1P\x1dKIC OPAMP\x1dQ25\x1d1T5397196ML5"
        "\x1d9D2524+5\x1d4LUS\x1e\x04"
    )
    parsed = labels.parse_label(payload)

    assert parsed["supplier"] == "DigiKey"
    assert parsed["distributor_pn"] == "296-OPA211AIDRGRCT-ND"
