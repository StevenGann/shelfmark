"""The scan panel goes through the browser session, not the API.

``/api/*`` accepts API keys only, so a logged-in engineer calling it from the
page would get 401. These tests exist to keep that from happening again: they
drive the same form the browser posts.
"""

import io

import zxingcpp
from PIL import Image


def _label_image(payload: str) -> bytes:
    """Render a Data Matrix as a PNG, upscaled so it survives the decoder."""
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.DataMatrix)
    raw = barcode.to_image()
    height, width = raw.shape
    image = Image.frombytes("L", (width, height), bytes(raw)).convert("RGB")
    image = image.resize((width * 8, height * 8), Image.Resampling.NEAREST)
    pad = Image.new("RGB", (image.width + 80, image.height + 80), "white")
    pad.paste(image, (40, 40))
    out = io.BytesIO()
    pad.save(out, format="PNG")
    return out.getvalue()


OPA211 = (
    "[)>\x1e06\x1dP296-OPA211AIDRGRCT-ND\x1d1POPA211AIDRGR\x1d30P296-OPA211AIDRGRCT-ND"
    "\x1dKCAN AND THERMOMETER\x1d9D2524+5\x1d1T5397196ML5\x1d4LDE\x1dQ25\x1d11ZPICK"
)


def test_scan_form_decodes_and_prefills_the_receipt(user_client):
    from app import db
    from app.repo import parts as repo

    conn = db.connect()
    try:
        part = repo.create_part(conn, mpn="OPA211AIDRGR", manufacturer="Texas Instruments")
    finally:
        conn.close()

    r = user_client.post(
        "/stock/scan",
        files={"file": ("label.png", _label_image(OPA211), "image/png")},
    )

    assert r.status_code == 200
    body = r.text
    assert "296-OPA211AIDRGRCT-ND" in body
    assert part["ipn"] in body
    assert 'value="25"' in body
    assert "5397196ML5" in body
    assert "2524+5" in body


def test_confirming_a_known_label_receives_stock_at_the_suggested_location(user_client):
    from app import db
    from app.repo import parts as repo
    from app.repo import stock as stock_repo

    conn = db.connect()
    try:
        part = repo.create_part(conn, mpn="OPA211AIDRGR", manufacturer="Texas Instruments")
        stock_repo.record_move(conn, int(part["id"]), "Drawer A", "receive", 3, None, "tester")
    finally:
        conn.close()

    scan = user_client.post(
        "/stock/scan",
        files={"file": ("label.png", _label_image(OPA211), "image/png")},
    )
    assert scan.status_code == 200
    # The confirm form carries the matched IPN and the suggested location; the
    # location datalist on the page would also contain "Drawer A", so assert on
    # the hidden inputs instead.
    assert f'name="ipn" value="{part["ipn"]}"' in scan.text
    assert 'name="location" list="locations"' in scan.text
    assert 'value="25"' in scan.text

    r = user_client.post(
        "/stock/label-receive",
        data={
            "ipn": part["ipn"],
            "mpn": "OPA211AIDRGR",
            "digikey_pn": "296-OPA211AIDRGRCT-ND",
            "lot": "5397196ML5",
            "date_code": "2524+5",
            "location": "Drawer A",
            "quantity": "25",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    conn = db.connect()
    try:
        rows = stock_repo.get_stock(conn, int(part["id"]))
        move = conn.execute(
            "SELECT note FROM stock_move WHERE stock_item_id = ? ORDER BY id DESC LIMIT 1",
            (rows[0]["id"],),
        ).fetchone()
    finally:
        conn.close()
    assert rows[0]["quantity"] == 28
    assert move["note"] == "lot 5397196ML5, DC 2524+5"


def test_unknown_label_creates_the_part_and_links_its_digikey_number(user_client):
    r = user_client.post(
        "/stock/label-receive",
        data={
            "mpn": "OPA211AIDRGR",
            "description": "CAN AND THERMOMETER",
            "digikey_pn": "296-OPA211AIDRGRCT-ND",
            "lot": "5397196ML5",
            "date_code": "2524+5",
            "manufacturer": "Texas Instruments",
            "location": "Bin 12",
            "quantity": "25",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    from app import db

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT p.ipn, l.sku, m.name FROM part p "
            "JOIN part_link l ON l.part_id = p.id "
            "JOIN manufacturer m ON m.id = p.manufacturer_id "
            "WHERE p.mpn = 'OPA211AIDRGR'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["sku"] == "296-OPA211AIDRGRCT-ND"
    assert row["name"] == "Texas Instruments"


def test_a_photo_without_a_barcode_says_so_and_keeps_the_form(user_client):
    blank = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(blank, format="PNG")

    r = user_client.post(
        "/stock/scan",
        files={"file": ("blank.png", blank.getvalue(), "image/png")},
    )

    assert r.status_code == 422
    assert "No barcode found" in r.text
    assert 'action="/stock/scan"' in r.text


def test_a_non_image_upload_is_refused(user_client):
    r = user_client.post(
        "/stock/scan",
        files={"file": ("label.png", b"not an image at all", "image/png")},
    )

    assert r.status_code == 422
    assert "Expected a JPEG, PNG or WebP image" in r.text


def test_a_viewer_sees_no_scan_panel(anon_client, make_user):
    user = make_user("viewer")
    login = anon_client.post(
        "/login",
        data={"username": user["username"], "password": user["password"], "next": "/"},
        follow_redirects=False,
    )
    anon_client.cookies.set("smk_session", login.cookies["smk_session"])

    r = anon_client.get("/stock")

    assert r.status_code == 200
    assert "Scan label" not in r.text
