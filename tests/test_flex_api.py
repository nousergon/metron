"""IBKR Flex import endpoint (PH1d) — broker-reported positions through the API.

Flex is snapshot-sourced (positions + pre-computed lots + cash, no trade history), so
this exercises the *other* half of the holdings model: positions land in the
``positions`` table and surface in the holdings view unioned with any ledger-derived
(CSV/OFX) holdings. The network fetch is monkeypatched to the connector's own test
fixture — no IBKR call.
"""

from __future__ import annotations

import io
import uuid

import pytest

from tests.test_ibkr_flex_connector import STATEMENT

FLEX_ACCT = "U33333333"  # matches the connector test fixture


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


@pytest.fixture()
def flex_ok(monkeypatch):
    """Make the endpoint's real connector parse the fixture instead of hitting IBKR."""
    monkeypatch.setattr(
        "portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml",
        lambda *a, **k: STATEMENT,
    )


def _new_portfolio(client, tenant, name="Brokerage"):
    r = client.post("/portfolios", json={"name": name}, headers={"X-Tenant-Id": tenant})
    assert r.status_code == 201
    return r.json()["id"]


def _sync_flex(client, tenant, pid, token="tok", query_id="qid"):
    return client.post(
        f"/portfolios/{pid}/import/flex",
        json={"token": token, "query_id": query_id},
        headers={"X-Tenant-Id": tenant},
    )


def test_flex_sync_persists_positions_and_cash(client, tenant, flex_ok):
    pid = _new_portfolio(client, tenant)
    r = _sync_flex(client, tenant, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "ibkr_flex"
    assert body["positions_imported"] == 2          # RKLB + SPY (OPT/CASH skipped by the connector)
    assert body["transactions_inserted"] == 4       # dividend + interest recv/paid + withholding

    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    assert set(holdings) == {"RKLB", "SPY"}
    assert holdings["RKLB"]["quantity"] == 100 and holdings["RKLB"]["avg_cost"] == 20
    assert holdings["SPY"]["quantity"] == 10


def test_flex_resync_replaces_positions_not_duplicates(client, tenant, flex_ok):
    pid = _new_portfolio(client, tenant)
    _sync_flex(client, tenant, pid)
    second = _sync_flex(client, tenant, pid).json()
    # Positions are a snapshot → replaced each sync (no ghosts, no doubling); cash
    # transactions are events → unioned by source_key (all already seen).
    assert second["positions_imported"] == 2
    assert second["transactions_inserted"] == 0 and second["transactions_skipped"] == 4
    holdings = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
    assert {h["ticker"] for h in holdings} == {"RKLB", "SPY"}  # still 2, not 4


def test_holdings_union_ledger_and_broker_positions(client, tenant, flex_ok):
    # One portfolio, two ingestion models: a CSV account (transaction-derived AAPL) +
    # a Flex account (broker-reported RKLB/SPY) → holdings unions all three.
    pid = _new_portfolio(client, tenant)
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(b"date,type,symbol,quantity,price\n2024-01-15,BUY,AAPL,5,150\n"), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    _sync_flex(client, tenant, pid)
    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    assert set(holdings) == {"AAPL", "RKLB", "SPY"}
    assert holdings["AAPL"]["quantity"] == 5


def test_flex_fetch_failure_returns_502(client, tenant, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("token rejected")

    monkeypatch.setattr("portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml", _boom)
    pid = _new_portfolio(client, tenant)
    r = _sync_flex(client, tenant, pid)
    assert r.status_code == 502
    assert "token rejected" in r.json()["detail"]


def test_flex_requires_tenant_ownership(client, tenant, flex_ok):
    pid = _new_portfolio(client, tenant)
    other = str(uuid.uuid4())
    assert _sync_flex(client, other, pid).status_code == 404
