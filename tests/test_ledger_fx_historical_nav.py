"""Historical NAV for foreign holdings in ledger-replayed (CSV/OFX) accounts (metron-ops-I389).

Reproduced against ``main`` before this file existed: a CSV account buys 10 ``0700.HK`` at
300 HKD and 10 ``AAPL`` at $100, with closes and an HKD→USD rate of 0.128 cached. The ledger
account has no lots, so ``_ticker_currencies`` (lot tables only) defaulted ``0700.HK`` to USD
and every reconstructed point was **4000.0** — the HKD close entered NAV unconverted, and
the buy's flow summed 3000 HKD + 1000 USD as 4000. Converted, it is 10 × 300 × 0.128 + 1000
= **1384.0**, which is what today's live valuation already said.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.db import models
from api.services import analytics, performance
from api.services import fx as fx_service
from api.services import prices as price_service

TODAY = date(2024, 3, 20)
BUY = date(2024, 1, 2)
RATE = 0.128
CONVERTED = 10 * 300 * RATE + 10 * 100  # 1384.0
UNCONVERTED = 10 * 300 + 10 * 100  # 4000.0 — the pre-fix value


def _days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _seed(session, *, rate_from: date | None = BUY, second_buy: date | None = None):
    """One CSV account (no lots) holding a HKD stock and a USD stock, with daily closes
    and, from ``rate_from`` (None = never), a daily HKD→USD rate in the caches."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    hk = models.Security(symbol="0700.HK", currency="HKD")
    us = models.Security(symbol="AAPL", currency="USD")
    spy = models.Security(symbol="SPY", currency="USD")
    session.add_all([acct, hk, us, spy])
    session.flush()
    txns = [
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=hk.id, txn_type="BUY",
            quantity=10, price=300.0, amount=3000.0, currency="HKD", trade_date=BUY, source_key="hk",
        ),
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=us.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD", trade_date=BUY, source_key="us",
        ),
    ]
    if second_buy is not None:
        txns.append(models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=hk.id, txn_type="BUY",
            quantity=10, price=300.0, amount=3000.0, currency="HKD", trade_date=second_buy, source_key="hk2",
        ))
    session.add_all(txns)
    for d in _days(BUY, TODAY):
        session.add_all([
            models.PriceBar(security_id=hk.id, bar_date=d, close=300.0, currency="HKD"),
            models.PriceBar(security_id=us.id, bar_date=d, close=100.0, currency="USD"),
            models.PriceBar(security_id=spy.id, bar_date=d, close=480.0, currency="USD"),
        ])
        if rate_from is not None and d >= rate_from:
            session.add(models.FxRate(currency="HKD", base="USD", rate_date=d, rate=RATE))
    session.commit()
    return tenant.id, pf.id, acct.id


@pytest.fixture
def no_fetch(monkeypatch):
    """Value from the seeded caches only — no price or FX network backfill."""
    monkeypatch.setattr(price_service, "backfill_prices", lambda *a, **k: 0)
    monkeypatch.setattr(fx_service, "backfill_fx_rates", lambda *a, **k: 0)


def _stored(session, pid):
    return session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid).order_by(models.NavSnapshot.snap_date)
    ).all()


def _points(session, tenant_id, pid):
    return performance._reconstruct_nav_points(
        session, tenant_id, pid, account_ids=None, today=TODAY, backfill=False
    )


def test_ledger_tickers_resolve_their_currency_from_the_portfolios_own_security_links(db_session):
    tenant_id, pid, _acct = _seed(db_session)
    assert performance._ticker_currencies(db_session, tenant_id, pid) == {"0700.HK": "HKD", "AAPL": "USD"}


def test_a_lot_currency_still_wins_over_a_security_link(db_session):
    tenant_id, pid, acct = _seed(db_session)
    db_session.add(models.OpenLot(
        tenant_id=tenant_id, account_id=acct, ticker="AAPL", quantity=1, cost_basis=100.0,
        open_date=BUY, currency="EUR",
    ))
    db_session.commit()
    assert performance._ticker_currencies(db_session, tenant_id, pid)["AAPL"] == "EUR"


def test_reconstructed_nav_converts_a_csv_foreign_holding(db_session, no_fetch):
    """The issue's reproduction: every point was 4000.0; it is 1384.0."""
    tenant_id, pid, _acct = _seed(db_session)
    performance.reconstruct_snapshots(db_session, tenant_id, pid, today=TODAY)
    rows = _stored(db_session, pid)
    assert rows
    for r in rows:
        assert float(r.nav) == pytest.approx(CONVERTED), f"{r.snap_date}: {float(r.nav)} (pre-fix {UNCONVERTED})"
        assert not r.estimated_inputs, f"{r.snap_date} had as-of inputs and is exact"
    # The buy's flow is in base currency too: 3000 HKD × 0.128 + $1000, not 4000.
    assert float(rows[0].external_flow) == pytest.approx(CONVERTED)
    assert all(float(r.external_flow) == pytest.approx(0.0) for r in rows[1:])


def test_a_mid_history_foreign_buy_is_no_spurious_step_and_meets_todays_live_point(db_session, no_fetch):
    """Flat prices and a flat rate: every sub-period's flow-neutral return is zero — the
    second HKD buy's NAV step is exactly its converted flow — and the last reconstructed
    point equals today's live valuation."""
    tenant_id, pid, _acct = _seed(db_session, second_buy=date(2024, 2, 15))
    points = _points(db_session, tenant_id, pid)
    assert len(points) > 2
    step = next(p for p in points if p.flow)
    assert step.when == BUY
    later = [p for p in points if p.when > BUY and p.flow]
    assert [p.flow for p in later] == [pytest.approx(10 * 300 * RATE)]
    for prev, cur in zip(points, points[1:], strict=False):
        assert (cur.nav - cur.flow) / prev.nav - 1 == pytest.approx(0.0, abs=1e-12), f"spurious return at {cur.when}"
    live = sum(h.market_value for h in analytics.valued_holdings(db_session, tenant_id, pid) if h.market_value)
    assert points[-1].nav == pytest.approx(live)
    assert live == pytest.approx(20 * 300 * RATE + 1000)


def test_a_flow_converted_at_a_later_rate_is_stamped_as_an_estimate(db_session):
    """Only rates from 2024-02-01 exist: the BUY-date flow (and NAV leg) use the later
    rate and say so; points from 2024-02-01 are exact."""
    tenant_id, pid, _acct = _seed(db_session, rate_from=date(2024, 2, 1))
    points = _points(db_session, tenant_id, pid)
    first = points[0]
    assert first.when == BUY
    assert first.flow == pytest.approx(CONVERTED)
    assert {"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date", "leg": "flow"} in (
        first.estimated_inputs
    )
    assert all(not p.estimated_inputs for p in points if p.when >= date(2024, 2, 1))


def test_a_flow_with_no_rate_at_all_is_excluded_like_its_nav_leg(db_session):
    """No HKD rate: the HKD leg is excluded from NAV (never valued at parity), so its flow
    is excluded too — otherwise the TWR would read the missing leg as a loss."""
    tenant_id, pid, _acct = _seed(db_session, rate_from=None)
    first = _points(db_session, tenant_id, pid)[0]
    assert first.nav == pytest.approx(1000.0)
    assert first.flow == pytest.approx(1000.0)
    assert {"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "unavailable", "leg": "flow"} in (
        first.estimated_inputs
    )


def test_forward_recorded_flows_use_the_same_conversion(db_session):
    """``record_snapshot`` / ``record_account_snapshots`` / the snapshot-gap reader total
    the same column — one rule for both paths (issue comment)."""
    tenant_id, pid, acct = _seed(db_session)
    assert performance._net_purchases(db_session, tenant_id, pid, after=None, through=TODAY) == pytest.approx(
        CONVERTED
    )
    assert performance._account_net_purchases(
        db_session, tenant_id, acct, after=None, through=TODAY
    ) == pytest.approx(CONVERTED)


def test_a_forward_flow_at_a_later_rate_is_reported(db_session):
    tenant_id, pid, _acct = _seed(db_session, rate_from=date(2024, 2, 1))
    estimates: list[dict] = []
    flow = performance._net_purchases(db_session, tenant_id, pid, after=None, through=BUY, estimates=estimates)
    assert flow == pytest.approx(CONVERTED)
    assert estimates == [{"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date"}]
