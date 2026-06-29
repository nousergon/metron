"""record_snapshot marks a snapshot PROVISIONAL and persists its priced composition when a
held mutual fund's NAV hasn't struck yet (its cached close predates snap_date)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.db import models
from api.services import performance
from portfolio_analytics.prices import ClosePoint

TODAY = date(2024, 6, 3)
YESTERDAY = TODAY - timedelta(days=1)


@pytest.fixture(autouse=True)
def _stub_spy(monkeypatch):
    monkeypatch.setattr(
        "api.services.performance.fetch_latest_closes",
        lambda syms, *, source=None: {"SPY": ClosePoint(bar_date=TODAY, close=500.0)},
    )


def _seed(session, *, fund_bar: date, equity_bar: date):
    """A SnapTrade-shaped account holding FNILX (fund) + AAPL (equity), each priced from a
    cached close on the given bar date. fund_bar < TODAY makes FNILX a stale fund leg."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade",
        external_id="FID-1", institution="Fidelity", currency="USD",
    )
    fnilx = models.Security(symbol="FNILX", currency="USD", asset_class="fund")
    aapl = models.Security(symbol="AAPL", currency="USD", asset_class="equity")
    session.add_all([acct, fnilx, aapl])
    session.flush()
    session.add_all([
        models.Position(tenant_id=tenant.id, account_id=acct.id, security_id=fnilx.id,
                        quantity=100, avg_cost=18.0, currency="USD", as_of=TODAY),
        models.Position(tenant_id=tenant.id, account_id=acct.id, security_id=aapl.id,
                        quantity=10, avg_cost=150.0, currency="USD", as_of=TODAY),
        models.PriceBar(security_id=fnilx.id, bar_date=fund_bar, close=20.0, currency="USD"),
        models.PriceBar(security_id=aapl.id, bar_date=equity_bar, close=200.0, currency="USD"),
    ])
    session.commit()
    return tenant.id, pf.id


def test_stale_fund_leg_marks_snapshot_provisional_with_composition(db_session):
    tenant_id, pid = _seed(db_session, fund_bar=YESTERDAY, equity_bar=TODAY)
    row = performance.record_snapshot(db_session, tenant_id, pid, today=TODAY)
    assert row is not None
    # NAV = 100×20 (stale fund close) + 10×200 = 4000.
    assert row.nav == pytest.approx(4000.0)
    assert row.provisional is True
    legs = {leg["ticker"]: leg for leg in row.composition["legs"]}
    assert legs["FNILX"]["is_fund"] and legs["FNILX"]["stale"] is True
    assert legs["FNILX"]["proxy"] == "SPY"
    assert legs["AAPL"]["is_fund"] is False and legs["AAPL"]["stale"] is False
    # value == qty × price × fx, and the legs sum to NAV.
    assert legs["FNILX"]["value"] == pytest.approx(2000.0)
    assert sum(leg["value"] for leg in row.composition["legs"]) == pytest.approx(row.nav)


def test_fresh_fund_leg_is_not_provisional(db_session):
    # Fund close already struck for TODAY → not stale → snapshot is final.
    tenant_id, pid = _seed(db_session, fund_bar=TODAY, equity_bar=TODAY)
    row = performance.record_snapshot(db_session, tenant_id, pid, today=TODAY)
    assert row.provisional is False
    legs = {leg["ticker"]: leg for leg in row.composition["legs"]}
    assert legs["FNILX"]["stale"] is False
