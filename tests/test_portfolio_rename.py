"""GET one portfolio + PATCH rename — tenant-scoped, validated."""

from __future__ import annotations

import uuid


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _create(client, tenant, name="Old name"):
    return client.post("/portfolios", json={"name": name}, headers=_hdr(tenant)).json()["id"]


def test_get_single_portfolio(client):
    t = str(uuid.uuid4())
    pid = _create(client, t, "My book")
    r = client.get(f"/portfolios/{pid}", headers=_hdr(t))
    assert r.status_code == 200
    assert r.json()["name"] == "My book"


def test_rename_updates_name_everywhere(client):
    t = str(uuid.uuid4())
    pid = _create(client, t, "Old name")
    r = client.patch(f"/portfolios/{pid}", json={"name": "  Brokerage  "}, headers=_hdr(t))
    assert r.status_code == 200
    assert r.json()["name"] == "Brokerage"  # trimmed
    # reflected in the single GET and the list
    assert client.get(f"/portfolios/{pid}", headers=_hdr(t)).json()["name"] == "Brokerage"
    names = [p["name"] for p in client.get("/portfolios", headers=_hdr(t)).json()]
    assert names == ["Brokerage"]


def test_rename_empty_name_is_422(client):
    t = str(uuid.uuid4())
    pid = _create(client, t)
    r = client.patch(f"/portfolios/{pid}", json={"name": "   "}, headers=_hdr(t))
    assert r.status_code == 422


def test_get_and_rename_cross_tenant_is_404(client):
    t = str(uuid.uuid4())
    pid = _create(client, t)
    other = _hdr(str(uuid.uuid4()))
    assert client.get(f"/portfolios/{pid}", headers=other).status_code == 404
    assert client.patch(f"/portfolios/{pid}", json={"name": "x"}, headers=other).status_code == 404
