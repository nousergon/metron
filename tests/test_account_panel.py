"""Accounts panel + per-account filtering (the account-selection arc).

Covers the new surface: per-account valuation on ``GET /accounts``, the repeatable
``?account_id=`` selection scoping holdings / summary / realized / tax, the
ownership-validating dependency (404 on a foreign/unknown id, absent → whole portfolio),
the nickname + 3-way ``tax_treatment`` Settings edits, and the per-account NAV snapshot
accrual. A multi-account portfolio is built via the CSV ``account`` column.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from portfolio_analytics.prices import ClosePoint

# Roth: AAPL 10 @100 (cost 1000). Taxable: MSFT 5 @200 (cost 1000). Portfolio cost 2000.
CSV = """date,type,symbol,quantity,price,amount,account
2024-01-01,BUY,AAPL,10,100,1000,Roth
2024-01-01,BUY,MSFT,5,200,1000,Taxable
"""

# Priced: AAPL 150 → Roth MV 1500 (+500); MSFT 300 → Taxable MV 1500 (+500).
_CLOSES = {"AAPL": ClosePoint(bar_date=date(2024, 6, 3), close=150.0),
           "MSFT": ClosePoint(bar_date=date(2024, 6, 3), close=300.0)}
_SPY = ClosePoint(bar_date=date(2024, 6, 3), close=500.0)


def _price_src(symbols, *, source=None):
    return {s: _CLOSES[s] for s in symbols if s in _CLOSES}


def _spy_src(symbols, *, source=None):
    return {"SPY": _SPY} if "SPY" in symbols else {}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant, csv=CSV):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def _accounts(client, tenant, pid):
    return client.get(f"/portfolios/{pid}/accounts", headers=_hdr(tenant)).json()


def _acct_id(client, tenant, pid, external_id):
    return next(a["account_id"] for a in _accounts(client, tenant, pid) if a["external_id"] == external_id)


def _refresh(client, tenant, pid, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))


class TestAccountValuation:
    def test_accounts_carry_cost_basis_and_nickname_fields(self, client, tenant):
        pid = _seed(client, tenant)
        accts = {a["external_id"]: a for a in _accounts(client, tenant, pid)}
        # Price-free: cost basis present per account, market value still null (no fabrication).
        assert accts["Roth"]["cost_basis_base"] == 1000
        assert accts["Taxable"]["cost_basis_base"] == 1000
        assert accts["Roth"]["market_value"] is None
        assert accts["Roth"]["unrealized_gain"] is None
        assert accts["Roth"]["nickname"] is None
        assert accts["Roth"]["n_unconverted"] == 0

    def test_accounts_value_after_price_refresh(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        accts = {a["external_id"]: a for a in _accounts(client, tenant, pid)}
        assert accts["Roth"]["market_value"] == 1500
        assert accts["Roth"]["unrealized_gain"] == 500
        assert accts["Taxable"]["market_value"] == 1500
        assert accts["Taxable"]["unrealized_gain"] == 500

    def test_per_account_valuation_sums_to_portfolio(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        accts = _accounts(client, tenant, pid)
        summary = client.get(f"/portfolios/{pid}/summary", headers=_hdr(tenant)).json()
        assert sum(a["market_value"] for a in accts) == summary["market_value"] == 3000


class TestAccountsMeta:
    """GET /accounts/meta (metron-ops#91 Part 2) — cacheable selector metadata, no
    valuation fields, so a short-TTL frontend cache carries no stale-NAV risk."""

    def test_meta_carries_tag_fields_not_valuation(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)  # priced, so a leaking valuation field would be non-null
        meta = {a["external_id"]: a for a in client.get(f"/portfolios/{pid}/accounts/meta", headers=_hdr(tenant)).json()}
        for field in ("account_id", "broker", "external_id", "name", "currency", "nickname", "institution",
                      "account_type", "tax_treatment", "taxable"):
            assert field in meta["Roth"]
        for field in ("cost_basis_base", "market_value", "unrealized_gain", "n_unconverted",
                      "overnight_pct", "intraday_pct", "day_pct", "ytd_pct", "ltm_pct"):
            assert field not in meta["Roth"]

    def test_meta_matches_accounts_metadata_subset(self, client, tenant):
        pid = _seed(client, tenant)
        full = {a["external_id"]: a for a in _accounts(client, tenant, pid)}
        meta = {a["external_id"]: a for a in client.get(f"/portfolios/{pid}/accounts/meta", headers=_hdr(tenant)).json()}
        assert full.keys() == meta.keys()
        for key, m in meta.items():
            f = full[key]
            for field in ("account_id", "broker", "external_id", "name", "currency", "nickname",
                          "institution", "account_type", "tax_treatment", "taxable"):
                assert m[field] == f[field]

    def test_meta_reflects_tag_edits(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        client.patch(f"/portfolios/{pid}/accounts/{roth}", json={"nickname": "My Roth"}, headers=_hdr(tenant))
        meta = {a["external_id"]: a for a in client.get(f"/portfolios/{pid}/accounts/meta", headers=_hdr(tenant)).json()}
        assert meta["Roth"]["nickname"] == "My Roth"

    def test_meta_foreign_portfolio_404(self, client, tenant):
        pid_a = _seed(client, tenant)
        other_tenant = str(uuid.uuid4())
        pid_b = _seed(client, other_tenant)
        r = client.get(f"/portfolios/{pid_b}/accounts/meta", headers=_hdr(tenant))
        assert r.status_code == 404
        assert client.get(f"/portfolios/{pid_a}/accounts/meta", headers=_hdr(tenant)).status_code == 200


class TestSelectionScoping:
    def test_holdings_scoped_to_one_account(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        h = client.get(f"/portfolios/{pid}/holdings", params={"account_id": roth}, headers=_hdr(tenant)).json()
        assert [x["ticker"] for x in h] == ["AAPL"]

    def test_summary_subset_and_union(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        taxable = _acct_id(client, tenant, pid, "Taxable")
        one = client.get(f"/portfolios/{pid}/summary", params={"account_id": roth}, headers=_hdr(tenant)).json()
        assert one["total_cost_basis"] == 1000 and one["n_accounts"] == 1
        both = client.get(
            f"/portfolios/{pid}/summary", params={"account_id": [roth, taxable]}, headers=_hdr(tenant)
        ).json()
        whole = client.get(f"/portfolios/{pid}/summary", headers=_hdr(tenant)).json()
        # Explicit both == absent (whole portfolio): no double-count, no drop.
        assert both["total_cost_basis"] == whole["total_cost_basis"] == 2000

    def test_realized_scoped(self, client, tenant):
        # Roth has a realized lot; Taxable has none.
        csv = (
            "date,type,symbol,quantity,price,amount,account\n"
            "2024-01-01,BUY,AAPL,10,100,1000,Roth\n"
            "2024-06-01,SELL,AAPL,4,150,600,Roth\n"
            "2024-01-01,BUY,MSFT,5,200,1000,Taxable\n"
        )
        pid = _seed(client, tenant, csv=csv)
        taxable = _acct_id(client, tenant, pid, "Taxable")
        roth = _acct_id(client, tenant, pid, "Roth")
        assert client.get(f"/portfolios/{pid}/realized", params={"account_id": taxable}, headers=_hdr(tenant)).json() == []
        roth_lots = client.get(f"/portfolios/{pid}/realized", params={"account_id": roth}, headers=_hdr(tenant)).json()
        assert len(roth_lots) == 1 and roth_lots[0]["ticker"] == "AAPL"


class TestOwnershipDependency:
    def test_unknown_account_id_404(self, client, tenant):
        pid = _seed(client, tenant)
        r = client.get(f"/portfolios/{pid}/holdings", params={"account_id": str(uuid.uuid4())}, headers=_hdr(tenant))
        assert r.status_code == 404

    def test_one_known_one_unknown_404(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        r = client.get(
            f"/portfolios/{pid}/holdings", params={"account_id": [roth, str(uuid.uuid4())]}, headers=_hdr(tenant)
        )
        assert r.status_code == 404

    def test_foreign_portfolio_account_404(self, client, tenant):
        pid_a = _seed(client, tenant)
        # Portfolio B with a DISTINCT account name (accounts are unique per
        # tenant+broker+external_id, so a reused name would collide rather than create a
        # second account).
        csv_b = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,NVDA,2,500,1000,Other\n"
        pid_b = _seed(client, tenant, csv=csv_b)
        other_b = _acct_id(client, tenant, pid_b, "Other")
        # An account that exists, but in a DIFFERENT portfolio — must 404 (no cross-portfolio leak).
        r = client.get(f"/portfolios/{pid_a}/holdings", params={"account_id": other_b}, headers=_hdr(tenant))
        assert r.status_code == 404

    def test_absent_selection_is_whole_portfolio(self, client, tenant):
        pid = _seed(client, tenant)
        h = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
        assert {x["ticker"] for x in h} == {"AAPL", "MSFT"}


class TestSettingsTags:
    def test_set_nickname_and_three_way_treatment(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        r = client.patch(
            f"/portfolios/{pid}/accounts/{roth}",
            json={"nickname": "My Roth", "tax_treatment": "tax_exempt"},
            headers=_hdr(tenant),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["nickname"] == "My Roth"
        assert body["tax_treatment"] == "tax_exempt"
        assert body["taxable"] is False  # tax_exempt → not taxable

    def test_setting_treatment_clears_taxable_override(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        # First force a taxable_override=True…
        client.patch(f"/portfolios/{pid}/accounts/{roth}", json={"taxable_override": True}, headers=_hdr(tenant))
        # …then set the 3-way to tax_deferred; the override must be cleared so the 3-way wins.
        body = client.patch(
            f"/portfolios/{pid}/accounts/{roth}", json={"tax_treatment": "tax_deferred"}, headers=_hdr(tenant)
        ).json()
        assert body["taxable"] is False  # would be True if the stale override still applied

    def test_invalid_treatment_rejected(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        r = client.patch(
            f"/portfolios/{pid}/accounts/{roth}", json={"tax_treatment": "roth-ish"}, headers=_hdr(tenant)
        )
        assert r.status_code == 422

    def test_nickname_survives_reimport(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        client.patch(f"/portfolios/{pid}/accounts/{roth}", json={"nickname": "Keep Me"}, headers=_hdr(tenant))
        # Re-import the same CSV (a routine re-sync) — the user nickname must not be clobbered.
        client.post(
            f"/portfolios/{pid}/import/csv",
            files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
            headers=_hdr(tenant),
        )
        roth_acct = next(a for a in _accounts(client, tenant, pid) if a["account_id"] == roth)
        assert roth_acct["nickname"] == "Keep Me"


class TestTaxIntersect:
    def test_taxable_only_intersects_selection(self, client, tenant):
        pid = _seed(client, tenant)
        roth = _acct_id(client, tenant, pid, "Roth")
        client.patch(f"/portfolios/{pid}/accounts/{roth}", json={"tax_treatment": "tax_exempt"}, headers=_hdr(tenant))
        # Select ONLY the tax-exempt account with taxable_only on → intersection is empty,
        # the account is excluded (taxable-only safety wins over the selection).
        r = client.get(
            f"/portfolios/{pid}/tax", params={"account_id": roth, "taxable_only": "true"}, headers=_hdr(tenant)
        ).json()
        assert r["n_lots"] == 0
        assert r["n_accounts_excluded"] >= 1


class TestAccountSnapshotAccrual:
    def test_refresh_records_per_account_snapshots(self, client, db_session, tenant, monkeypatch):
        import sqlalchemy as sa

        from api.db import models

        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        rows = db_session.scalars(sa.select(models.AccountNavSnapshot)).all()
        # One per account (both priced), each holding that account's NAV.
        navs = sorted(float(r.nav) for r in rows)
        assert navs == [1500.0, 1500.0]

    def test_account_snapshots_idempotent_per_day(self, client, db_session, tenant, monkeypatch):
        import sqlalchemy as sa

        from api.db import models

        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        _refresh(client, tenant, pid, monkeypatch)  # second refresh same day
        n = db_session.scalar(
            sa.select(sa.func.count()).select_from(models.AccountNavSnapshot)
        )
        assert n == 2  # still one row per account, not duplicated
