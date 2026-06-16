"""User-set security labels (metron-ops#47) — a readable alias for an opaque numeric
CUSIP, plumbed onto holdings, tenant-scoped, clearable."""

from __future__ import annotations

import io
import uuid

import pytest

CSV = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,912828YK0,50,98,4900,Brokerage\n"


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed(client, tenant: str) -> str:
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    return pid


def _label_of(client, tenant, pid, symbol):
    holdings = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    return next((h["user_label"] for h in holdings if h["ticker"] == symbol), "MISSING")


def test_set_get_clear_label(client, tenant):
    pid = _seed(client, tenant)
    assert _label_of(client, tenant, pid, "912828YK0") is None  # unset by default

    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/label", json={"label": "US Treasury 2026"}, headers=_hdr(tenant)
    )
    assert r.status_code == 200
    assert r.json() == {"symbol": "912828YK0", "label": "US Treasury 2026"}
    assert _label_of(client, tenant, pid, "912828YK0") == "US Treasury 2026"

    # Clearing with an empty label removes it.
    r = client.put(f"/portfolios/{pid}/securities/912828YK0/label", json={"label": "  "}, headers=_hdr(tenant))
    assert r.json()["label"] is None
    assert _label_of(client, tenant, pid, "912828YK0") is None


def test_label_lowercase_symbol_normalized(client, tenant):
    pid = _seed(client, tenant)
    client.put(f"/portfolios/{pid}/securities/912828yk0/label", json={"label": "Treasury"}, headers=_hdr(tenant))
    assert _label_of(client, tenant, pid, "912828YK0") == "Treasury"


def test_label_is_tenant_scoped(client, tenant):
    pid = _seed(client, tenant)
    client.put(f"/portfolios/{pid}/securities/912828YK0/label", json={"label": "Mine"}, headers=_hdr(tenant))
    other = str(uuid.uuid4())
    # Another tenant can't even see this portfolio (404 — never leaks cross-tenant).
    assert client.put(
        f"/portfolios/{pid}/securities/912828YK0/label", json={"label": "Hijack"}, headers=_hdr(other)
    ).status_code == 404
