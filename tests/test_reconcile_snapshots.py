"""reconcile_snapshots restates a PROVISIONAL NAV snapshot once the held fund's true struck
NAV lands — swapping the stale leg's price, re-summing NAV, and finalizing the row."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from api.db import models
from api.services import performance

SNAP = date(2024, 6, 3)
PRIOR = SNAP - timedelta(days=1)
NEXT = SNAP + timedelta(days=1)


def _seed_provisional(session, *, snap_date=SNAP, fund_qty=100, stale_close=20.0,
                      equity_value=2000.0, fund_symbol="FNILX"):
    """A provisional portfolio + account snapshot whose fund leg is stale (priced off the
    PRIOR session's close). No struck bar for snap_date yet."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade",
                          external_id="FID-1", currency="USD")
    fnilx = models.Security(symbol=fund_symbol, currency="USD", asset_class="fund")
    session.add_all([acct, fnilx])
    session.flush()
    comp = {"schema": 1, "legs": [
        {"ticker": fund_symbol, "qty": fund_qty, "price": stale_close,
         "price_date": PRIOR.isoformat(), "currency": "USD", "fx_rate": 1.0,
         "value": fund_qty * stale_close, "is_fund": True, "stale": True, "proxy": "SPY"},
        {"ticker": "AAPL", "qty": 10, "price": 200.0, "price_date": snap_date.isoformat(),
         "currency": "USD", "fx_rate": 1.0, "value": equity_value, "is_fund": False,
         "stale": False, "proxy": None},
    ]}
    nav = fund_qty * stale_close + equity_value
    session.add_all([
        models.NavSnapshot(tenant_id=tenant.id, portfolio_id=pf.id, snap_date=snap_date,
                           nav=nav, cost_basis=0, external_flow=0, provisional=True, composition=comp),
        models.AccountNavSnapshot(tenant_id=tenant.id, portfolio_id=pf.id, account_id=acct.id,
                                  snap_date=snap_date, nav=nav, cost_basis=0, external_flow=0,
                                  provisional=True, composition=comp),
    ])
    session.commit()
    return tenant.id, pf.id, fnilx.id


def _struck(session, security_id, when, close):
    session.add(models.PriceBar(security_id=security_id, bar_date=when, close=close, currency="USD"))
    session.commit()


def test_restates_and_finalizes_once_fund_strikes(db_session):
    tenant_id, pid, fnilx_id = _seed_provisional(db_session)
    _struck(db_session, fnilx_id, SNAP, 21.0)  # the true struck NAV for the snapshot session
    n = performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT)
    assert n == 2  # portfolio + account row
    row = db_session.scalars(
        performance.select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)
    ).first()
    leg = next(leg for leg in row.composition["legs"] if leg["ticker"] == "FNILX")
    assert leg["price"] == 21.0 and leg["price_date"] == SNAP.isoformat() and leg["stale"] is False
    # NAV restated: 100×21 (struck) + 2000 = 4100 (was 4000 with the stale 20.0).
    assert row.nav == pytest.approx(4100.0)
    assert row.provisional is False


def test_idempotent_second_run_noops(db_session):
    tenant_id, pid, fnilx_id = _seed_provisional(db_session)
    _struck(db_session, fnilx_id, SNAP, 21.0)
    assert performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT) == 2
    assert performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT) == 0


def test_unstruck_fund_stays_provisional(db_session):
    tenant_id, pid, fnilx_id = _seed_provisional(db_session)
    # No struck bar written → nothing to restate, row stays provisional.
    assert performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT) == 0
    row = db_session.scalars(
        performance.select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)
    ).first()
    assert row.provisional is True and row.nav == pytest.approx(4000.0)


def test_snapshot_older_than_window_is_left_alone(db_session):
    old = SNAP - timedelta(days=30)
    tenant_id, pid, fnilx_id = _seed_provisional(db_session, snap_date=old)
    _struck(db_session, fnilx_id, old, 21.0)
    # `today` is far past the 7-day reconcile window relative to `old`.
    assert performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT) == 0
    row = db_session.scalars(
        performance.select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)
    ).first()
    assert row.provisional is True


def test_restatement_is_a_tiny_delta_never_a_spike(db_session):
    """A stale→struck fund move is sub-percent — restatement must not move NAV like the
    implausible-jump guard's 3× threshold (the guard is deliberately bypassed here)."""
    tenant_id, pid, fnilx_id = _seed_provisional(db_session, stale_close=20.0)
    _struck(db_session, fnilx_id, SNAP, 20.1)  # +0.5% on the fund
    performance.reconcile_snapshots(db_session, tenant_id, pid, today=NEXT)
    row = db_session.scalars(
        performance.select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)
    ).first()
    assert row.nav == pytest.approx(4010.0)  # 100×20.1 + 2000


def test_bounded_fetch_equals_unbounded_fetch(db_session, monkeypatch):
    """metron-ops-I279: reconcile_snapshots now fetches the fund's close history bounded
    to [min(recorded price_date), max(snap_date)] instead of the whole price_bars table.

    Two structurally-identical scenarios (different fund tickers, so both can share one
    DB/session without colliding on the ``(symbol, currency)`` uniqueness), each seeded
    with DECOY bars strictly outside that window — dated before every leg's recorded
    price_date, and after every snapshot's date — with prices that would corrupt the
    restated NAV if a bound-computation bug let them leak in (or dropped a real one).

    Scenario A runs through the real (bounded) ``close_history_by_symbol``. Scenario B
    monkeypatches it back to the pre-I279 unbounded call. The restated NAV, provisional
    flag and struck price/date must come out byte-identical either way."""
    from api.services import prices as prices_module

    def _run(session, *, fund_symbol):
        tenant_id, pid, fund_id = _seed_provisional(session, fund_symbol=fund_symbol)
        _struck(session, fund_id, SNAP, 21.0)  # the true struck close for SNAP
        # Decoys: outside [PRIOR, SNAP] on both ends, so the `recorded < bar_date <=
        # snap` filter can never select them under EITHER fetch mode — proof that
        # bounding didn't change which bar wins.
        session.add_all([
            models.PriceBar(security_id=fund_id, bar_date=PRIOR - timedelta(days=5),
                            close=999.0, currency="USD"),
            models.PriceBar(security_id=fund_id, bar_date=SNAP + timedelta(days=5),
                            close=1.0, currency="USD"),
        ])
        session.commit()
        n = performance.reconcile_snapshots(session, tenant_id, pid, today=NEXT)
        row = session.scalars(
            performance.select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)
        ).first()
        leg = next(leg for leg in row.composition["legs"] if leg["ticker"] == fund_symbol)
        return n, row.nav, row.provisional, leg["price"], leg["price_date"]

    bounded = _run(db_session, fund_symbol="FNILX")

    real = prices_module.close_history_by_symbol

    def _unbounded(session, symbols, *, start_date=None, end_date=None):
        return real(session, symbols)  # ignore the bound — the pre-I279 behaviour

    monkeypatch.setattr(prices_module, "close_history_by_symbol", _unbounded)
    unbounded = _run(db_session, fund_symbol="FXAIX")

    assert bounded == unbounded
