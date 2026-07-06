"""Per-holding tearsheet (metron-ops#22) — Position + Performance + Technical from data
Metron already has; the fundamentals blocks are honestly N/A until the spine artifact ships.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.db import models
from api.services import tearsheet
from portfolio_analytics.prices import ClosePoint


def _seed(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    aapl = models.Security(symbol="AAPL", currency="USD")
    spy = models.Security(symbol="SPY", currency="USD")
    session.add_all([acct, aapl, spy])
    session.flush()
    # 10 sh AAPL @ $100 = $1,000 cost.
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=aapl.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2025, 1, 1), source_key="buy-aapl",
        )
    )
    # 70 daily bars for AAPL (up-trending, both up/down days) and SPY.
    start = date(2025, 1, 1)
    av, sv = 100.0, 400.0
    for i in range(70):
        av *= 1.01 if i % 2 == 0 else 0.995
        sv *= 1.008 if i % 2 == 0 else 0.997
        d = start + timedelta(days=i)
        session.add(models.PriceBar(security_id=aapl.id, bar_date=d, close=av, currency="USD"))
        session.add(models.PriceBar(security_id=spy.id, bar_date=d, close=sv, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def test_tearsheet_position_and_performance(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL")
    assert sheet is not None
    # Position
    assert sheet.position.ticker == "AAPL"
    assert sheet.position.quantity == 10
    assert sheet.position.cost_basis == pytest.approx(1000.0)
    assert sheet.position.market_value is not None and sheet.position.market_value > 1000.0
    assert sheet.position.unrealized_pct is not None and sheet.position.unrealized_pct > 0
    assert sheet.position.weight_pct == pytest.approx(1.0)  # the only holding
    assert sheet.position.accounts == ["CSV-1"]
    # Performance from the 70-bar history (>= the 60-bar risk floor)
    assert sheet.performance.n_bars == 70
    assert sheet.performance.return_vs_cost is not None
    assert sheet.performance.volatility is not None
    assert sheet.performance.sharpe is not None
    assert sheet.performance.sortino is not None
    assert sheet.performance.max_drawdown is not None
    assert sheet.performance.beta_vs_spy is not None  # SPY bars overlap fully
    assert sheet.performance.vs_spy is not None
    # Technical
    assert sheet.technical.rsi_14 is not None
    assert sheet.technical.pct_from_52wk_high is not None
    assert sheet.technical.forward_div_yield is None  # fundamentals
    # Fundamentals honestly gated
    assert sheet.fundamentals_available is False
    assert "1022" in sheet.fundamentals_reason


_FUND_ART = {
    "as_of": "2026-06-17",
    "fundamentals": {
        "AAPL": {
            "sector": "Technology", "industry": "Consumer Electronics", "marketCap": 3.2e12, "beta": 1.2,
            "trailingPE": 30.0, "forwardPE": 28.0, "enterpriseToEbitda": 22.0,
            "earningsGrowth": 0.1, "revenueGrowth": 0.08, "debtToEquity": 150.0,
            "currentRatio": 1.1, "quickRatio": 0.9, "returnOnEquity": 0.5, "returnOnAssets": 0.2,
            "grossMargins": 0.44, "operatingMargins": 0.30, "dividendYield": 0.5,
        }
    },
}


def test_tearsheet_fundamentals_populate_when_feed_enabled(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=True, fundamentals_reader=lambda: _FUND_ART
    )
    assert sheet.fundamentals_available is True
    assert sheet.fundamentals is not None
    assert sheet.fundamentals.trailing_pe == 30.0
    assert sheet.fundamentals.peg == pytest.approx(3.0)            # 30 / (0.1 * 100)
    assert sheet.fundamentals.dividend_yield == pytest.approx(0.005)  # 0.5% → fraction
    assert sheet.technical.forward_div_yield == pytest.approx(0.005)
    # AAPL is its own comp row, flagged is_self.
    assert any(c.is_self and c.ticker == "AAPL" for c in sheet.comps)


def test_tearsheet_fundamentals_omitted_when_feed_off(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=False, fundamentals_reader=lambda: _FUND_ART
    )
    assert sheet.fundamentals_available is False
    assert sheet.fundamentals is None


def test_tearsheet_none_when_not_held(db_session):
    tenant_id, pid = _seed(db_session)
    assert tearsheet.tearsheet(db_session, tenant_id, pid, "TSLA") is None


def test_tearsheet_endpoint_404_for_unheld(client):
    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.get(f"/portfolios/{pid}/tearsheet/TSLA", headers={"X-Tenant-Id": tenant})
    assert r.status_code == 404


def _seed_crwd(session):
    """CRWD holding with a stale pre-split 1Y anchor in the local cache."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    crwd = models.Security(symbol="CRWD", currency="USD")
    session.add_all([acct, crwd])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=crwd.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2025, 1, 1), source_key="buy-crwd",
        )
    )
    # Stale local cache: pre-split anchor + correct latest (the failure mode Brian saw).
    session.add(models.PriceBar(security_id=crwd.id, bar_date=date(2025, 7, 6), close=518.0, currency="USD"))
    session.add(models.PriceBar(security_id=crwd.id, bar_date=date(2026, 7, 2), close=193.98, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def test_tearsheet_period_return_syncs_stale_history(db_session, monkeypatch):
    """Tearsheet must re-sync from the spine so a stale 1Y anchor doesn't persist."""
    tenant_id, pid = _seed_crwd(db_session)

    def spine_src(symbols, start, end, *, source=None):
        return {
            "CRWD": [
                ClosePoint(date(2025, 7, 6), 126.36),
                ClosePoint(date(2026, 7, 2), 193.98),
            ],
        }

    monkeypatch.setattr("api.services.prices.fetch_close_history", spine_src)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "CRWD")
    assert sheet is not None
    # Without the sync this would be 193.98/518 - 1 ≈ -62.5%; with corrected anchor ≈ +53.5%.
    assert sheet.performance.period_returns["1Y"] == pytest.approx(193.98 / 126.36 - 1.0, rel=1e-3)
