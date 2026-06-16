"""Watchlist (metron-ops#42) — positions-optional tracked tickers, read-only/illustrative
in the beta (reference data + held flag, no live price)."""

from __future__ import annotations

import io
import uuid

import pytest


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _portfolio(client, tenant: str) -> str:
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


def test_add_list_remove(client, tenant):
    pid = _portfolio(client, tenant)
    # Empty to start.
    assert client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(tenant)).json() == []

    # Add (lower-case input is normalized to upper).
    r = client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "nvda", "note": "AI"}, headers=_hdr(tenant))
    assert r.status_code == 201
    assert r.json()["symbol"] == "NVDA"
    assert r.json()["held"] is False  # un-held ticker
    assert r.json()["note"] == "AI"

    rows = client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(tenant)).json()
    assert [e["symbol"] for e in rows] == ["NVDA"]

    # Remove.
    d = client.delete(f"/portfolios/{pid}/watchlist/nvda", headers=_hdr(tenant))
    assert d.status_code == 200 and d.json()["removed"] is True
    assert client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(tenant)).json() == []


def test_add_is_idempotent_and_updates_note(client, tenant):
    pid = _portfolio(client, tenant)
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "AAPL"}, headers=_hdr(tenant))
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "AAPL", "note": "core"}, headers=_hdr(tenant))
    rows = client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(tenant)).json()
    assert len(rows) == 1
    assert rows[0]["note"] == "core"


def test_held_flag_reflects_holdings(client, tenant):
    pid = _portfolio(client, tenant)
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,MSFT,5,100,500,Brokerage\n"
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "MSFT"}, headers=_hdr(tenant))
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "TSLA"}, headers=_hdr(tenant))
    rows = {e["symbol"]: e["held"] for e in client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(tenant)).json()}
    assert rows["MSFT"] is True   # held
    assert rows["TSLA"] is False  # tracked but not held


def test_remove_missing_is_404(client, tenant):
    pid = _portfolio(client, tenant)
    assert client.delete(f"/portfolios/{pid}/watchlist/ZZZZ", headers=_hdr(tenant)).status_code == 404


def test_blank_symbol_is_422(client, tenant):
    pid = _portfolio(client, tenant)
    assert client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "  "}, headers=_hdr(tenant)).status_code == 422


def test_watchlist_is_tenant_scoped(client, tenant):
    pid = _portfolio(client, tenant)
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "AAPL"}, headers=_hdr(tenant))
    other = str(uuid.uuid4())
    # Another tenant can't see this portfolio (404 — never leaks cross-tenant).
    assert client.get(f"/portfolios/{pid}/watchlist", headers=_hdr(other)).status_code == 404
