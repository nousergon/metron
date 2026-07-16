"""Per-account drill-down — ``GET /portfolios/{id}/accounts/{account_id}`` (PH2).

A multi-account portfolio must break down cleanly: each account's holdings, realized
lots, and transactions scoped to that account alone (no bleed across accounts). The
``account`` CSV column splits one import into distinct accounts, which is exactly the
multi-account shape this view exists for.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest

from api.config import settings
from api.services import intraday as intraday_service
from api.services import security_perf

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

    def test_invalid_valuation_422(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _account_id(client, tenant, pid, "Roth")
        r = client.get(
            f"/portfolios/{pid}/accounts/{roth}?valuation=bogus", headers={"X-Tenant-Id": tenant}
        )
        assert r.status_code == 422


class TestAccountDetailLiveValuation:
    """``?valuation=live`` on the account drill-down (metron-ops#149 item 1): the account
    detail page mounts no ``LiveValuationProvider``/live overlay today, unlike the Holdings
    page (metron-ops#153/#79). Scoped to just this account's holdings, mirroring
    ``GET .../holdings?valuation=live``."""

    def _seed_live(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr(settings, "feed_entitled", True)
        # Reset the process-level snapshot TTL cache so this test's reader is honored.
        monkeypatch.setattr(intraday_service, "_snapshot_cache", None, raising=False)
        monkeypatch.setattr(intraday_service, "_snapshot_fetched_monotonic", 0.0, raising=False)

        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        client.put(
            f"/portfolios/{pid}/preferences", json={"intraday_enabled": True}, headers={"X-Tenant-Id": tenant}
        )
        csv = "date,type,symbol,quantity,price,amount,account\n2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
        r = client.post(
            f"/portfolios/{pid}/import/csv",
            files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200
        account_id = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()[0][
            "account_id"
        ]

        session_today = security_perf.market_today().isoformat()
        art = {
            "schema_version": 2,
            "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "quotes": {"AAPL": {"last": 130.0, "session_date": session_today}},
        }
        monkeypatch.setattr(intraday_service, "_default_reader", lambda: art)
        return pid, account_id

    def test_live_valuation_revalues_this_account(self, client, db_session, tenant, monkeypatch):
        pid, account_id = self._seed_live(client, db_session, tenant, monkeypatch)
        d = client.get(
            f"/portfolios/{pid}/accounts/{account_id}?valuation=live", headers={"X-Tenant-Id": tenant}
        ).json()
        assert d["holdings"][0]["last_price"] == pytest.approx(130.0)

    def test_settled_default_never_serves_live_values(self, client, db_session, tenant, monkeypatch):
        pid, account_id = self._seed_live(client, db_session, tenant, monkeypatch)
        d = client.get(f"/portfolios/{pid}/accounts/{account_id}", headers={"X-Tenant-Id": tenant}).json()
        assert d["holdings"][0]["last_price"] is None or d["holdings"][0]["last_price"] != 130.0
