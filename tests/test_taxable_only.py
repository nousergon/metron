"""taxable_only filter on income / realized / transactions (metron-ops#48).

Tax-advantaged accounts (IRA / 401k / Roth) generate no taxable income or gains, so the
Tax + Activity views can restrict to taxable accounts. An account named "IRA" derives to
non-taxable via keyword inference (no explicit tagging needed)."""

from __future__ import annotations

import io
import uuid

import pytest

# Brokerage (taxable) + IRA (tax-advantaged), each with a realized gain + a dividend.
CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-06-02,SELL,AAPL,10,150,1500,Brokerage\n"
    "2024-03-01,DIVIDEND,AAPL,0,0,20,Brokerage\n"
    "2024-01-02,BUY,MSFT,10,200,2000,IRA\n"
    "2024-06-02,SELL,MSFT,10,300,3000,IRA\n"
    "2024-03-01,DIVIDEND,MSFT,0,0,30,IRA\n"
)


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed(client, tenant: str) -> str:
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def test_income_taxable_only_excludes_tax_advantaged(client, tenant):
    pid = _seed(client, tenant)
    all_income = client.get(f"/portfolios/{pid}/income", headers=_hdr(tenant)).json()
    only = client.get(f"/portfolios/{pid}/income?taxable_only=true", headers=_hdr(tenant)).json()
    # Default (all accounts): both dividends counted (20 + 30 = 50).
    assert sum(y["dividends"] for y in all_income) == pytest.approx(50)
    # Taxable only: just the Brokerage dividend (20) — the IRA's is excluded.
    assert sum(y["dividends"] for y in only) == pytest.approx(20)


def test_realized_taxable_only_excludes_ira_lots(client, tenant):
    pid = _seed(client, tenant)
    all_lots = client.get(f"/portfolios/{pid}/realized", headers=_hdr(tenant)).json()
    only = client.get(f"/portfolios/{pid}/realized?taxable_only=true", headers=_hdr(tenant)).json()
    assert {r["ticker"] for r in all_lots} == {"AAPL", "MSFT"}
    assert {r["ticker"] for r in only} == {"AAPL"}  # MSFT (IRA) excluded


def test_transactions_taxable_only_excludes_ira(client, tenant):
    pid = _seed(client, tenant)
    only = client.get(f"/portfolios/{pid}/transactions?taxable_only=true", headers=_hdr(tenant)).json()
    assert {t["ticker"] for t in only} == {"AAPL"}  # no MSFT rows
