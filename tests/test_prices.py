"""EOD price cache → market value + unrealized P&L (C2-6a, personal tier).

The source (yfinance by default) is injected/monkeypatched here so the suite never
hits the network. The invariants under test: refresh is idempotent on (security, day);
a holding with a cached close gets market value + unrealized P&L; a holding WITHOUT one
stays cost-basis-only (None), never fabricated; the portfolio summary aggregates only
priced holdings.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from portfolio_analytics.prices import ClosePoint, fetch_latest_closes

# AAPL 10 @100 (cost 1000) + MSFT 5 @200 (cost 1000), both open.
CSV = """date,type,symbol,quantity,price
2024-01-01,BUY,AAPL,10,100
2024-01-01,BUY,MSFT,5,200
"""

# Fake source: prices AAPL only — MSFT is deliberately unpriceable, to prove the
# no-fabrication path (MSFT must stay cost-basis-only).
_AAPL_CLOSE = ClosePoint(bar_date=date(2024, 6, 3), close=150.0)


def _fake_source(symbols, *, source=None):
    return {s: _AAPL_CLOSE for s in symbols if s == "AAPL"}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant, csv=CSV):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200
    return pid


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


class TestFetchLatestCloses:
    def test_empty_input(self):
        assert fetch_latest_closes([]) == {}

    def test_dedup_and_passthrough(self):
        seen = []

        def src(symbols, *, source=None):
            seen.append(symbols)
            return dict.fromkeys(symbols, _AAPL_CLOSE)

        out = fetch_latest_closes(["AAPL", "AAPL", "MSFT", ""], source=src)
        assert seen == [["AAPL", "MSFT"]]  # deduped, blanks dropped
        assert set(out) == {"AAPL", "MSFT"}


class TestRefreshAndValue:
    def test_refresh_then_holdings_are_valued(self, client, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _fake_source)
        pid = _seed(client, tenant)

        r = client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        assert r.status_code == 200
        body = r.json()
        assert body["symbols_requested"] == 2 and body["prices_updated"] == 1  # only AAPL priced

        holdings = {h["ticker"]: h for h in client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()}
        aapl = holdings["AAPL"]
        assert aapl["last_price"] == 150.0
        assert aapl["last_price_date"] == "2024-06-03"
        assert aapl["market_value"] == 1500.0       # 150 * 10
        assert aapl["unrealized_gain"] == 500.0      # 1500 - 1000
        assert aapl["unrealized_pct"] == 0.5
        # MSFT was not priceable → stays cost-basis-only, never fabricated.
        msft = holdings["MSFT"]
        assert msft["market_value"] is None and msft["unrealized_gain"] is None

    def test_holdings_price_free_before_refresh(self, client, tenant):
        pid = _seed(client, tenant)
        for h in client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json():
            assert h["market_value"] is None and h["last_price"] is None

    def test_refresh_is_idempotent(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _fake_source)
        pid = _seed(client, tenant)
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        # Second refresh updates the same (security, day) bar — it does not duplicate.
        from sqlalchemy import func, select

        from api.db import models

        n_bars = db_session.scalar(select(func.count()).select_from(models.PriceBar))
        assert n_bars == 1

    def test_summary_aggregates_priced_holdings(self, client, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _fake_source)
        pid = _seed(client, tenant)
        # Before refresh: no market value.
        s0 = client.get(f"/portfolios/{pid}/summary", headers=_hdr(tenant)).json()
        assert s0["market_value"] is None and s0["unrealized_gain"] is None
        # After: only AAPL priced → MV 1500, unrealized 500.
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        s1 = client.get(f"/portfolios/{pid}/summary", headers=_hdr(tenant)).json()
        assert s1["market_value"] == 1500.0 and s1["unrealized_gain"] == 500.0

    def test_refresh_requires_tenant_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        other = str(uuid.uuid4())
        assert client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(other)).status_code == 404


class TestPriceServiceGuards:
    """Direct service-level edge paths the HTTP flow doesn't reach."""

    def test_refresh_empty_symbols(self, db_session):
        from api.services import prices

        assert prices.refresh_latest_prices(db_session, []) == 0

    def test_refresh_skips_symbol_with_no_security(self, db_session):
        from api.services import prices

        # Source prices a symbol that has no securities row in this DB → nothing written.
        def src(symbols, *, source=None):
            return {"ZZZZ": _AAPL_CLOSE}

        assert prices.refresh_latest_prices(db_session, ["ZZZZ"], source=src) == 0

    def test_latest_close_empty(self, db_session):
        from api.services import prices

        assert prices.latest_close_by_symbol(db_session, []) == {}
