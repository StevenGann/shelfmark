import pytest

from app import db
from app.repo import parts as parts_repo
from app.repo import stock as stock_repo


def _make_part(mpn="LM358N", manufacturer="TI"):
    conn = db.connect()
    try:
        return parts_repo.create_part(conn, mpn=mpn, manufacturer=manufacturer)
    finally:
        conn.close()


def _record(conn, part_id, location, kind, quantity, note=None, actor="tester"):
    return stock_repo.record_move(conn, part_id, location, kind, quantity, note, actor)


def _moves(conn, part_id):
    return conn.execute(
        "SELECT sm.* FROM stock_move sm "
        "JOIN stock_item si ON si.id = sm.stock_item_id "
        "WHERE si.part_id = ? ORDER BY sm.id",
        (part_id,),
    ).fetchall()


def test_receive_consume_count_math_and_ledger_deltas():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "Drawer A", "receive", 10, "incoming", "alice")
        _record(conn, pid, "Drawer A", "consume", 3, "kit A", "alice")
        _record(conn, pid, "Drawer A", "adjust", 12, "recount", "bob")

        items = stock_repo.get_stock(conn, pid)
        assert len(items) == 1
        assert items[0]["quantity"] == 12

        moves = _moves(conn, pid)
        assert [m["kind"] for m in moves] == ["receive", "consume", "adjust"]
        assert [m["delta"] for m in moves] == [10, -3, 5]
        assert [m["resulting_quantity"] for m in moves] == [10, 7, 12]
        assert [m["actor"] for m in moves] == ["alice", "alice", "bob"]
    finally:
        conn.close()


def test_consume_below_zero_refused_and_row_unchanged():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "A", "receive", 5, None, "alice")
        with pytest.raises(stock_repo.StockError):
            _record(conn, pid, "A", "consume", 6, None, "alice")

        assert stock_repo.total_on_hand(conn, pid) == 5
        assert len(_moves(conn, pid)) == 1
    finally:
        conn.close()


def test_adjust_to_counted_value():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "Bin", "receive", 20, None, "alice")
        _record(conn, pid, "Bin", "adjust", 4, "recount", "bob")

        item = stock_repo.get_stock(conn, pid)[0]
        assert item["quantity"] == 4
        moves = _moves(conn, pid)
        assert [m["delta"] for m in moves] == [20, -16]
        assert [m["resulting_quantity"] for m in moves] == [20, 4]
    finally:
        conn.close()


def test_move_merges_into_existing_destination():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "A", "receive", 10, None, "alice")
        _record(conn, pid, "B", "receive", 3, None, "alice")
        stock_repo.move_between(conn, pid, "A", "B", 4, "alice")

        items = {i["location"]: i["quantity"] for i in stock_repo.get_stock(conn, pid)}
        assert items == {"A": 6, "B": 7}
    finally:
        conn.close()


def test_location_norm_merges_two_spellings():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "Drawer A", "receive", 2, None, "alice")
        _record(conn, pid, "drawer   a", "receive", 3, None, "alice")

        items = stock_repo.get_stock(conn, pid)
        assert len(items) == 1
        assert items[0]["quantity"] == 5
    finally:
        conn.close()


def test_quantity_equals_sum_of_deltas_after_moves():
    part = _make_part()
    conn = db.connect()
    try:
        pid = int(part["id"])
        _record(conn, pid, "A", "receive", 10, None, "alice")
        _record(conn, pid, "A", "consume", 2, None, "alice")
        _record(conn, pid, "B", "receive", 5, None, "bob")
        stock_repo.move_between(conn, pid, "A", "B", 3, "bob")
        stock_repo.move_between(conn, pid, "B", "A", 1, "bob")

        total = stock_repo.total_on_hand(conn, pid)
        assert total == 13
        assert total == sum(m["delta"] for m in _moves(conn, pid))

        # Every row, not just the part: a move is double entry, so each stock
        # item's quantity is exactly the sum of that item's own deltas.
        for item in stock_repo.get_stock(conn, pid):
            ledger = sum(m["delta"] for m in _moves(conn, pid) if m["stock_item_id"] == item["id"])
            assert item["quantity"] == ledger
    finally:
        conn.close()


def test_api_receive_and_read_stock(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    r = client.post(
        f"/api/parts/{part['ipn']}/stock/moves",
        json={"location": "A", "kind": "receive", "quantity": 5},
    )
    assert r.status_code == 201

    got = client.get(f"/api/parts/{part['ipn']}/stock")
    assert got.status_code == 200
    body = got.json()
    assert body["total"] == 5
    assert body["items"][0]["location"] == "A"
    assert body["items"][0]["quantity"] == 5


def test_api_move_between_locations(client):
    part = client.post("/api/parts", json={"mpn": "NE555", "manufacturer": "TI"}).json()
    ipn = part["ipn"]
    client.post(
        f"/api/parts/{ipn}/stock/moves",
        json={"location": "A", "kind": "receive", "quantity": 10},
    )
    r = client.post(
        "/api/stock/move",
        json={"ipn": ipn, "from_location": "A", "to_location": "B", "quantity": 4},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from"]["quantity"] == 6
    assert body["to"]["quantity"] == 4


def test_api_insufficient_move_returns_409(client):
    part = client.post("/api/parts", json={"mpn": "LM358N", "manufacturer": "TI"}).json()
    r = client.post(
        f"/api/parts/{part['ipn']}/stock/moves",
        json={"location": "A", "kind": "consume", "quantity": 1},
    )
    assert r.status_code == 409


def test_api_post_by_viewer_is_403(anon_client, make_key):
    part = _make_part()
    key = make_key("viewer")
    r = anon_client.post(
        f"/api/parts/{part['ipn']}/stock/moves",
        json={"location": "A", "kind": "receive", "quantity": 5},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 403


def test_parts_list_shows_on_hand_total(user_client):
    part = _make_part()
    conn = db.connect()
    try:
        _record(conn, int(part["id"]), "Drawer A", "receive", 7, None, "alice")
    finally:
        conn.close()

    r = user_client.get("/")
    assert r.status_code == 200
    assert "On hand" in r.text
    assert "7</td>" in r.text
