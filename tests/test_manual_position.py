"""Manual position entry (metron-ops#187) — the third free-tier ingestion path,
alongside CSV/OFX. Covers the happy path (position appears in Holdings with
correct valuation, ``Account.broker == "manual"``), ticker/quantity validation
failures, and that it shares the same persistence bridge/behavior as CSV."""

from __future__ import annotations

import uuid

import pytest

from api.services import demo


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _new_portfolio(client, tenant, name="Taxable"):
    r = client.post("/portfolios", json={"name": name}, headers={"X-Tenant-Id": tenant})
    assert r.status_code == 201
    return r.json()["id"]


def _add_manual(client, tenant, pid, **overrides):
    body = {"ticker": "AAPL", "quantity": 10, "cost_basis": 1500, **overrides}
    return client.post(f"/portfolios/{pid}/positions/manual", json=body, headers={"X-Tenant-Id": tenant})


def test_manual_position_appears_in_holdings(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "manual"
    assert body["transactions_inserted"] == 1
    assert body["securities_created"] == 1
    assert body["accounts_created"] == 1

    holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    assert "AAPL" in holdings
    assert holdings["AAPL"]["quantity"] == 10
    # cost_basis=1500 total / 10 shares = $150/share; amount-fallback path (price=0).
    assert holdings["AAPL"]["avg_cost"] == pytest.approx(150.0)
    assert holdings["AAPL"]["cost_basis"] == pytest.approx(1500.0)


def test_manual_account_broker_is_manual(client, tenant):
    pid = _new_portfolio(client, tenant)
    _add_manual(client, tenant, pid)
    accounts = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()
    assert len(accounts) == 1
    assert accounts[0]["broker"] == "manual"


def test_second_manual_position_shares_manual_account(client, tenant):
    pid = _new_portfolio(client, tenant)
    _add_manual(client, tenant, pid, ticker="AAPL", quantity=10, cost_basis=1500)
    r = _add_manual(client, tenant, pid, ticker="MSFT", quantity=5, cost_basis=1000)
    assert r.status_code == 200
    assert r.json()["accounts_created"] == 0  # reused the existing "manual" account
    accounts = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()
    assert len(accounts) == 1
    holdings = {h["ticker"] for h in client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()}
    assert holdings == {"AAPL", "MSFT"}


def test_invalid_ticker_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, ticker="not a ticker!!")
    assert r.status_code == 422
    holdings = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
    assert holdings == []  # nothing partially persisted


def test_blank_ticker_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, ticker="")
    assert r.status_code == 422


def test_non_positive_quantity_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, quantity=0)
    assert r.status_code == 422
    r = _add_manual(client, tenant, pid, quantity=-5)
    assert r.status_code == 422


def test_negative_cost_basis_returns_422(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, cost_basis=-100)
    assert r.status_code == 422


@pytest.mark.parametrize("bad_quantity", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_quantity_returns_422_not_500(client, tenant, bad_quantity):
    # NaN/Infinity both silently pass a bare `quantity <= 0` check (NaN comparisons are
    # always False; inf > 0 is True) and would otherwise reach the DB's Numeric(28, 10)
    # column, which raises an unhandled 500 instead of a clean validation error.
    import json
    import math

    pid = _new_portfolio(client, tenant)
    # requests/httpx json= can't encode NaN/Infinity as strict JSON; post raw content
    # the same way Python's stdlib json.dumps (and a permissive client) would.
    payload = {"ticker": "AAPL", "quantity": bad_quantity, "cost_basis": 100}
    raw = json.dumps(payload, allow_nan=True)
    assert not math.isfinite(bad_quantity)  # sanity: this is the non-finite case
    r = client.post(
        f"/portfolios/{pid}/positions/manual",
        content=raw,
        headers={"X-Tenant-Id": tenant, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    holdings = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
    assert holdings == []


def test_absurdly_large_quantity_returns_422_not_500(client, tenant):
    # Finite but bigger than the Numeric(28, 10) column can hold — must 422 cleanly
    # rather than raise a DB numeric-overflow error mid-persist.
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, quantity=1e20)
    assert r.status_code == 422


def test_zero_cost_basis_allowed(client, tenant):
    # A gifted/inherited position with an unknown/zero basis is a legitimate use case —
    # not rejected, just yields a $0 avg_cost (matches CSV's "amount omitted" degenerate case).
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, cost_basis=0)
    assert r.status_code == 200


def test_optional_trade_date_recorded(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = _add_manual(client, tenant, pid, trade_date="2024-01-15")
    assert r.status_code == 200
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert len(txns) == 1
    assert txns[0]["trade_date"] == "2024-01-15"
    assert txns[0]["txn_type"] == "BUY"


def test_trade_date_defaults_to_today_when_omitted(client, tenant):
    import datetime

    pid = _new_portfolio(client, tenant)
    _add_manual(client, tenant, pid)
    txns = client.get(f"/portfolios/{pid}/transactions", headers={"X-Tenant-Id": tenant}).json()
    assert txns[0]["trade_date"] == datetime.date.today().isoformat()


def test_manual_position_requires_tenant_ownership(client, tenant):
    pid = _new_portfolio(client, tenant)
    other = str(uuid.uuid4())
    r = _add_manual(client, other, pid)
    assert r.status_code == 404


def test_manual_entry_shares_security_master_with_csv(client, tenant):
    # A ticker already known from a CSV import must not spawn a duplicate Security row —
    # same global upsert-by-(symbol,currency) bridge as every other ingestion source.
    import io

    pid = _new_portfolio(client, tenant)
    csv_text = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,1,100\n"
    csv_r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert csv_r.json()["securities_created"] == 1
    manual_r = _add_manual(client, tenant, pid, ticker="AAPL")
    assert manual_r.json()["securities_created"] == 0  # AAPL master already exists


def test_deleting_manual_account_removes_position(client, tenant):
    # Editing/removing reuses the existing account-deletion UI (no new deletion path) —
    # deleting the "manual" account removes its positions, same as any CSV account.
    pid = _new_portfolio(client, tenant)
    _add_manual(client, tenant, pid)
    accounts = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()
    account_id = accounts[0]["account_id"]
    r = client.delete(f"/portfolios/{pid}/accounts/{account_id}", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 200
    holdings = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
    assert holdings == []


def test_manual_entry_rejects_unknown_fields(client, tenant):
    pid = _new_portfolio(client, tenant)
    r = client.post(
        f"/portfolios/{pid}/positions/manual",
        json={"ticker": "AAPL", "quantity": 1, "cost_basis": 100, "extra_field": "nope"},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 422


def test_reference_portfolio_manual_add_is_read_only(client):
    # The showcase portfolio's read-only HTTP guard (api/main.py::_demo_read_only) is a
    # path-based check covering every mutating route under /portfolios/{id}/... uniformly
    # — confirm the new route is covered too, not just the pre-existing import routes.
    r = _add_manual(client, str(demo.DEMO_TENANT_ID), str(demo.REFERENCE_PORTFOLIO_ID))
    assert r.status_code == 403
