"""Uncombined (per-account) Holdings view (metron-ops#114).

Default ``GET …/holdings`` consolidates one row per ticker across accounts;
``?by_account=1`` returns one row per (account, ticker), each tagged with
account_id/account_label, and respects the ``?account_id=`` scope.
"""

from __future__ import annotations

import io
import uuid

import pytest

# AAPL is held in BOTH accounts (the case the uncombined view exists for); MSFT in one.
CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-01-02,BUY,MSFT,10,200,2000,Brokerage\n"
    "2024-01-02,BUY,AAPL,5,100,500,IRA\n"
)


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def _accounts(client, tenant, pid) -> dict[str, str]:
    rows = client.get(f"/portfolios/{pid}/accounts", headers=_hdr(tenant)).json()
    return {a["name"]: a["account_id"] for a in rows}


def test_consolidated_is_one_row_per_ticker(client, tenant):
    pid = _seed(client, tenant)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    by_ticker = {r["ticker"]: r for r in rows}
    assert set(by_ticker) == {"AAPL", "MSFT"}
    assert by_ticker["AAPL"]["quantity"] == pytest.approx(15.0)  # 10 + 5 across accounts
    # No account attribution on the consolidated view.
    assert by_ticker["AAPL"]["account_id"] is None
    assert by_ticker["AAPL"]["account_label"] is None


def test_by_account_splits_a_multi_account_ticker_into_rows(client, tenant):
    pid = _seed(client, tenant)
    rows = client.get(f"/portfolios/{pid}/holdings?by_account=1", headers=_hdr(tenant)).json()
    # One row per (account, ticker): AAPL×2 + MSFT×1.
    keyed = {(r["ticker"], r["account_label"]): r for r in rows}
    assert keyed[("AAPL", "Brokerage")]["quantity"] == pytest.approx(10.0)
    assert keyed[("AAPL", "IRA")]["quantity"] == pytest.approx(5.0)
    assert keyed[("MSFT", "Brokerage")]["quantity"] == pytest.approx(10.0)
    assert ("MSFT", "IRA") not in keyed
    # Every uncombined row carries its account attribution.
    for r in rows:
        assert r["account_id"] is not None
        assert r["account_label"] in {"Brokerage", "IRA"}


def test_by_account_respects_account_scope(client, tenant):
    pid = _seed(client, tenant)
    acct = _accounts(client, tenant, pid)
    rows = client.get(
        f"/portfolios/{pid}/holdings?by_account=1&account_id={acct['Brokerage']}",
        headers=_hdr(tenant),
    ).json()
    assert {(r["ticker"], r["account_label"]) for r in rows} == {
        ("AAPL", "Brokerage"),
        ("MSFT", "Brokerage"),
    }
