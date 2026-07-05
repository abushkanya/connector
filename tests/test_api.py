"""Phase 6: serve_as_api — REST CRUD over the database."""

import pytest
from fastapi.testclient import TestClient

from connector.api import build_app

KEY = "s3cret"


@pytest.fixture()
def client(db):
    db.users.add(username="alice", age=30, dept="eng") \
        .add(username="bob", age=25, dept="ops").exec()
    app = build_app(db, key=KEY)
    with TestClient(app) as c:
        c.headers["X-API-Key"] = KEY
        yield c


def test_auth_required(db):
    app = build_app(db, key=KEY)
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/", headers={"X-API-Key": KEY}).status_code == 200


def test_index_and_list(client):
    index = client.get("/").json()
    assert "users" in index["tables"]

    rows = client.get("/users").json()
    assert {r["username"] for r in rows} == {"alice", "bob"}

    # filters with type coercion and operator suffixes
    assert client.get("/users", params={"dept": "eng"}).json()[0]["username"] == "alice"
    assert client.get("/users", params={"age__more": "26"}).json()[0]["username"] == "alice"
    assert [r["username"] for r in client.get(
        "/users", params={"_order": "age", "_desc": "true"}).json()] == ["alice", "bob"]
    assert len(client.get("/users", params={"_limit": "1", "_page": "2"}).json()) == 1


def test_get_by_pk_and_404(client):
    row = client.get("/users/1").json()
    assert row["username"] == "alice"
    assert client.get("/users/999").status_code == 404
    assert client.get("/no_such_table").status_code == 404


def test_create_update_delete(client):
    created = client.post("/users", json={"username": "carol", "age": 40})
    assert created.status_code == 201
    cid = created.json()["id"]

    updated = client.patch(f"/users/{cid}", json={"age": 41})
    assert updated.json()["age"] == 41

    assert client.patch("/users/999", json={"age": 1}).status_code == 404

    assert client.delete(f"/users/{cid}").json() == {"deleted": 1}
    assert client.get(f"/users/{cid}").status_code == 404


def test_bad_requests(client):
    assert client.post("/users", json={"nope": 1}).status_code == 400
    assert client.get("/users", params={"age__teleports": "1"}).status_code == 400
    assert client.post("/users", json=[1, 2]).status_code == 400
    # malformed values are 400s, not 500s
    assert client.get("/users/abc").status_code == 400
    assert client.get("/users", params={"_limit": "abc"}).status_code == 400
    assert client.get("/users", params={"_limit": "5", "_page": "x"}).status_code == 400
