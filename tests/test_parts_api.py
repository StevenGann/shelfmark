def _create(client, mpn="LM358N", manufacturer="TI", **extra):
    payload = {"mpn": mpn, "manufacturer": manufacturer, **extra}
    r = client.post("/api/parts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_read_list_patch(client):
    part = _create(client)
    assert part["ipn"] == "SK-00001"
    assert part["mpn"] == "LM358N"
    assert part["mpn_norm"] == "LM358N"
    assert part["design_status"] == "active"

    got = client.get(f"/api/parts/{part['ipn']}")
    assert got.status_code == 200
    assert got.json()["ipn"] == "SK-00001"

    listed = client.get("/api/parts").json()
    assert listed["total"] == 1
    assert listed["items"][0]["ipn"] == "SK-00001"

    patched = client.patch(f"/api/parts/{part['ipn']}", json={"description": "dual op-amp"})
    assert patched.status_code == 200
    assert patched.json()["description"] == "dual op-amp"
    assert patched.json()["ipn"] == "SK-00001"


def test_patch_cannot_change_ipn(client):
    part = _create(client)
    patched = client.patch(f"/api/parts/{part['ipn']}", json={"ipn": "SK-99999", "notes": "nope"})
    assert patched.status_code == 200
    assert patched.json()["ipn"] == "SK-00001"


def test_duplicate_returns_409_with_existing_ipn(client):
    _create(client)
    r = client.post("/api/parts", json={"mpn": "lm358n", "manufacturer": "TI"})
    assert r.status_code == 409
    assert r.json()["detail"]["ipn"] == "SK-00001"


def test_missing_returns_404(client):
    r = client.get("/api/parts/SK-99999")
    assert r.status_code == 404
    r = client.patch("/api/parts/SK-99999", json={"description": "x"})
    assert r.status_code == 404


def test_invalid_create_returns_422(client):
    r = client.post("/api/parts", json={"mpn": "LM358N"})
    assert r.status_code == 422


def test_pagination_totals(client):
    for i in range(5):
        _create(client, mpn=f"MPN-{i}", manufacturer="Acme")
    page = client.get("/api/parts", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 5
    assert page["limit"] == 2
    assert page["offset"] == 0
    assert len(page["items"]) == 2

    last = client.get("/api/parts", params={"limit": 2, "offset": 4}).json()
    assert len(last["items"]) == 1


def test_search_and_category_filter(client):
    _create(client, mpn="NE555", manufacturer="TI", description="timer", category="IC")
    _create(client, mpn="LM358N", manufacturer="TI", description="op-amp", category="IC")
    _create(client, mpn="RES-10K", manufacturer="Yageo", description="resistor", category="R")

    found = client.get("/api/parts", params={"q": "timer"}).json()
    assert found["total"] == 1
    assert found["items"][0]["mpn"] == "NE555"

    ics = client.get("/api/parts", params={"category": "IC"}).json()
    assert ics["total"] == 2
