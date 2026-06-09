"""End-to-end PH1 gate: a stranger's exported CSV round-trips to a correct
Portfolio (holdings) + realized view through the HTTP API."""

from __future__ import annotations

import io
import uuid

import pytest

CSV = """date,type,symbol,quantity,price,amount,fees
2024-01-15,BUY,AAPL,10,150,1500,1
2024-02-15,BUY,MSFT,5,300,1500,1
2024-03-01,DIVIDEND,AAPL,,,4.40,
2024-08-01,SELL,AAPL,4,200,800,1
"""


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _new_portfolio(client, tenant, name="Taxable"):
    r = client.post("/portfolios", json={"name": name}, headers={"X-Tenant-Id": tenant})
    assert r.status_code == 201
    return r.json()["id"]


def _upload(client, tenant, pid, text=CSV):
    return client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("trades.csv", io.BytesIO(text.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )


def test_csv_roundtrips_to_holdings(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload(client, tenant, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["rows_parsed"] == 4
    assert body["transactions_inserted"] == 4
    assert body["securities_created"] == 2

    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    # 10 AAPL bought, 4 sold (FIFO) → 6 left at $150 basis; 5 MSFT at $300.
    assert holdings["AAPL"]["quantity"] == 6
    assert holdings["AAPL"]["avg_cost"] == pytest.approx(150.1, abs=0.05)  # $1 fee folded into basis
    assert holdings["MSFT"]["quantity"] == 5


def test_realized_gain_computed(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    realized = client.get(f"/portfolios/{pid}/realized", headers={"X-Tenant-Id": tenant}).json()
    assert len(realized) == 1
    lot = realized[0]
    assert lot["ticker"] == "AAPL"
    assert lot["quantity"] == 4
    # Sold 4 @ $200 less $1 fee = $799 proceeds; basis 4 @ ~$150.1 = ~$600.4 → ~$198.6 gain.
    assert lot["gain"] == pytest.approx(198.6, abs=0.5)
    assert lot["long_term"] is False


def test_transactions_listed(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert [t["txn_type"] for t in txns] == ["BUY", "BUY", "DIVIDEND", "SELL"]


def test_reupload_is_idempotent(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    second = _upload(client, tenant, pid)
    assert second.json()["transactions_inserted"] == 0
    assert second.json()["transactions_skipped"] == 4
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert len(txns) == 4  # no duplication


def test_dirty_rows_reported_not_fatal(client, tenant):
    pid = _new_portfolio(client, tenant)
    dirty = "date,type,symbol,quantity,price\n2024-01-15,BUY,AAPL,1,100\nbad,BUY,AAPL,1,100\n"
    r = _upload(client, tenant, pid, dirty)
    assert r.status_code == 200
    assert r.json()["rows_parsed"] == 1
    assert r.json()["rows_skipped"] == 1
    assert r.json()["errors"][0]["line"] == 3


def test_invalid_csv_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _upload(client, tenant, pid, "symbol,quantity\nAAPL,1\n")  # no date/type columns
    assert r.status_code == 422


def test_import_requires_tenant_ownership(client, tenant):
    pid = _new_portfolio(client, tenant)
    other = str(uuid.uuid4())
    # Another tenant cannot import into — or even see — this portfolio (404, not 403).
    r = _upload(client, other, pid)
    assert r.status_code == 404
    assert client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": other}).status_code == 404


def test_holdings_isolated_per_tenant(client, tenant):
    pid = _new_portfolio(client, tenant)
    _upload(client, tenant, pid)
    other = str(uuid.uuid4())
    other_pid = _new_portfolio(client, other, name="Other")
    assert client.get(f"/portfolios/{other_pid}/holdings", headers={"X-Tenant-Id": other}).json() == []
