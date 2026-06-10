"""Per-account drill-down — ``GET /portfolios/{id}/accounts/{account_id}`` (PH2).

A multi-account portfolio must break down cleanly: each account's holdings, realized
lots, and transactions scoped to that account alone (no bleed across accounts). The
``account`` CSV column splits one import into distinct accounts, which is exactly the
multi-account shape this view exists for.
"""

from __future__ import annotations

import io
import uuid

import pytest

# Roth: AAPL bought 10 @100, sold 4 @150 → 6 open (basis 600) + 1 realized lot (ST +200).
# Taxable: MSFT 5 @200 open (basis 1000), nothing realized.
CSV = """date,type,symbol,quantity,price,amount,account
2024-01-01,BUY,AAPL,10,100,1000,Roth
2024-06-01,SELL,AAPL,4,150,600,Roth
2024-01-01,BUY,MSFT,5,200,1000,Taxable
"""


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant, csv=CSV, name="Brokerage"):
    pid = client.post("/portfolios", json={"name": name}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200
    return pid


def _accounts(client, tenant, pid):
    return client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()


def _account_id(client, tenant, pid, external_id):
    return next(a["account_id"] for a in _accounts(client, tenant, pid) if a["external_id"] == external_id)


def _detail(client, tenant, pid, account_id):
    return client.get(f"/portfolios/{pid}/accounts/{account_id}", headers={"X-Tenant-Id": tenant})


class TestAccountList:
    def test_accounts_expose_id(self, client, tenant):
        pid = _seed(client, tenant)
        for a in _accounts(client, tenant, pid):
            # additive field — present and a valid UUID the drill-down navigates by.
            uuid.UUID(a["account_id"])


class TestAccountDetail:
    def test_roth_is_scoped_to_its_own_activity(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _account_id(client, tenant, pid, "Roth")
        d = _detail(client, tenant, pid, roth).json()

        assert d["account"]["external_id"] == "Roth"
        # holdings: only AAPL (6 open), no MSFT bleed from Taxable.
        assert [h["ticker"] for h in d["holdings"]] == ["AAPL"]
        assert d["holdings"][0]["quantity"] == 6
        assert d["holdings"][0]["cost_basis"] == 600
        # one realized lot, the AAPL partial sale.
        assert len(d["realized"]) == 1
        assert d["realized"][0]["ticker"] == "AAPL" and d["realized"][0]["gain"] == 200
        # only Roth's two transactions.
        assert len(d["transactions"]) == 2
        assert {t["txn_type"] for t in d["transactions"]} == {"BUY", "SELL"}

    def test_taxable_is_scoped_to_its_own_activity(self, client, tenant):
        pid = _seed(client, tenant)
        taxable = _account_id(client, tenant, pid, "Taxable")
        d = _detail(client, tenant, pid, taxable).json()

        assert [h["ticker"] for h in d["holdings"]] == ["MSFT"]
        assert d["holdings"][0]["cost_basis"] == 1000
        assert d["realized"] == []
        assert len(d["transactions"]) == 1

    def test_unknown_account_404(self, client, tenant):
        pid = _seed(client, tenant)
        assert _detail(client, tenant, pid, str(uuid.uuid4())).status_code == 404

    def test_cross_tenant_404(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _account_id(client, tenant, pid, "Roth")
        assert _detail(client, str(uuid.uuid4()), pid, roth).status_code == 404

    def test_account_from_another_portfolio_404(self, client, tenant):
        pid_a = _seed(client, tenant, name="A")
        pid_b = _seed(client, tenant, name="B")
        roth_a = _account_id(client, tenant, pid_a, "Roth")
        # a real account id, but not under portfolio B → 404 (never leak across portfolios).
        assert _detail(client, tenant, pid_b, roth_a).status_code == 404
