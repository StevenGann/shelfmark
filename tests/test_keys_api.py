def test_create_list_revoke(client):
    r = client.post("/api/keys", json={"name": "laptop", "role": "engineer"})
    assert r.status_code == 201
    body = r.json()
    plaintext = body["key"]
    assert plaintext.startswith("smk_")
    assert body["name"] == "laptop"
    assert body["role"] == "engineer"
    assert isinstance(body["id"], int)

    listed = client.get("/api/keys").json()["items"]
    row = next(k for k in listed if k["name"] == "laptop")
    assert row["role"] == "engineer"
    assert row["prefix"] == plaintext[4:12]
    assert "key" not in row
    assert "key_hash" not in row
    assert plaintext not in str(listed)

    assert client.get("/api/parts", headers={"X-API-Key": plaintext}).status_code == 200

    assert client.post(f"/api/keys/{body['id']}/revoke").status_code == 204
    assert client.get("/api/parts", headers={"X-API-Key": plaintext}).status_code == 401
    assert client.post(f"/api/keys/{body['id']}/revoke").status_code == 204


def test_duplicate_name_returns_409(client):
    assert client.post("/api/keys", json={"name": "ci", "role": "viewer"}).status_code == 201
    r = client.post("/api/keys", json={"name": "ci", "role": "viewer"})
    assert r.status_code == 409


def test_invalid_role_returns_422(client):
    r = client.post("/api/keys", json={"name": "bad", "role": "superuser"})
    assert r.status_code == 422
