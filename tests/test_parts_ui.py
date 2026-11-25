def test_create_through_form_and_follow_redirect(client):
    r = client.post(
        "/parts/new",
        data={"mpn": "LM358N", "manufacturer": "TI", "design_status": "active"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "SK-00001" in r.text

    detail = client.get("/parts/SK-00001")
    assert detail.status_code == 200
    assert "LM358N" in detail.text

    listing = client.get("/")
    assert listing.status_code == 200
    assert "SK-00001" in listing.text


def test_form_rerenders_with_error_on_duplicate(client):
    first = client.post(
        "/parts/new",
        data={"mpn": "LM358N", "manufacturer": "TI"},
        follow_redirects=True,
    )
    assert first.status_code == 200

    r = client.post(
        "/parts/new",
        data={"mpn": "lm358n", "manufacturer": "TI"},
    )
    assert r.status_code == 200
    assert "already exists" in r.text
    assert "SK-00001" in r.text
    assert 'value="lm358n"' in r.text


def test_form_rerenders_on_validation_error(client):
    r = client.post("/parts/new", data={"mpn": "", "manufacturer": "TI"})
    assert r.status_code == 200
    assert "MPN is required" in r.text
    assert 'value="TI"' in r.text
