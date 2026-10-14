"""backfill-snapshot-gap: missed daily NAV snapshots are re-priced from each day's own closes,
only where positions are provably unchanged across the gap (metron-ops#361)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api import maintenance
from api.db import models
from api.services import snapshot_gap

A_DAY = date(2026, 10, 5)   # last snapshot before the gap (Mon)
GAP = (date(2026, 10, 6), date(2026, 10, 12))
B_DAY = date(2026, 10, 13)  # first snapshot after the gap (Tue)
# Weekday closes; the weekend (10-10, 10-11) carries Friday's.
AAPL = {date(2026, 10, d): 200.0 + d for d in (5, 6, 7, 8, 9, 12, 13)}
SPY = {d: 500.0 + d.day for d in AAPL}


def _leg(ticker, qty, price, *, value=None, ccy="USD", fx=1.0):
    return {"ticker": ticker, "qty": qty, "price": price, "price_date": None, "currency": ccy,
            "fx_rate": fx, "value": value if value is not None else qty * price * fx,
            "is_fund": False, "stale": False, "proxy": None}


def _snap(model, tenant, pf, day, legs, *, account=None, nav=None, flow=0.0):
    extra = {"account_id": account.id} if account is not None else {}
    return model(
        tenant_id=tenant.id, portfolio_id=pf.id, snap_date=day,
        nav=nav if nav is not None else sum(leg["value"] for leg in legs),
        cost_basis=1500.0, external_flow=flow, provisional=False,
        composition={"schema": 1, "legs": legs}, **extra,
    )


def _seed(session, *, a_legs=None, b_legs=None, aapl_bars=AAPL, b_nav=None, extra_securities=()):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="ibkr",
                          external_id="U1", institution="IBKR", currency="USD")
    aapl = models.Security(symbol="AAPL", currency="USD", asset_class="equity")
    spy = models.Security(symbol="SPY", currency="USD", asset_class="etf")
    session.add_all([acct, aapl, spy, *extra_securities])
    session.flush()
    session.add_all([models.PriceBar(security_id=aapl.id, bar_date=d, close=c, currency="USD")
                     for d, c in aapl_bars.items()])
    session.add_all([models.PriceBar(security_id=spy.id, bar_date=d, close=c, currency="USD")
                     for d, c in SPY.items()])
    a_legs = a_legs or [_leg("AAPL", 10, AAPL[A_DAY])]
    b_legs = b_legs or [_leg("AAPL", 10, AAPL[B_DAY])]
    for model, account in ((models.NavSnapshot, None), (models.AccountNavSnapshot, acct)):
        session.add(_snap(model, tenant, pf, A_DAY, a_legs, account=account))
        session.add(_snap(model, tenant, pf, B_DAY, b_legs, account=account, nav=b_nav))
    session.commit()
    return tenant, pf, acct


def _plan(session):
    return snapshot_gap.plan_gap(session, start=GAP[0], end=GAP[1])


def test_unchanged_positions_are_priced_from_each_days_own_close(db_session):
    _seed(db_session)
    plans = _plan(db_session)
    assert {p.grain for p in plans} == {"portfolio", "account"}
    for p in plans:
        assert p.skipped is None
        assert p.self_check == pytest.approx(0.0)
        # Every day in the gap, each priced from its own close — weekend carries Friday's.
        assert p.points == {
            d: pytest.approx(10 * AAPL.get(d, AAPL[date(2026, 10, 9)]))
            for d in (GAP[0] + timedelta(days=i) for i in range(7))
        }
        assert p.points[date(2026, 10, 7)] != p.points[date(2026, 10, 6)]  # not copied forward


def test_apply_inserts_final_zero_flow_marked_rows_and_is_idempotent(db_session):
    _seed(db_session)
    assert snapshot_gap.apply_plan(db_session, _plan(db_session)) == 14  # 7 days x 2 grains
    rows = db_session.scalars(select(models.NavSnapshot).order_by(models.NavSnapshot.snap_date)).all()
    assert [r.snap_date for r in rows] == [A_DAY, *(GAP[0] + timedelta(days=i) for i in range(7)), B_DAY]
    gap_rows = rows[1:-1]
    assert all(float(r.external_flow) == 0.0 and r.provisional is False for r in gap_rows)
    assert all(r.composition["backfill"]["by"] == snapshot_gap.BACKFILL_MARKER for r in gap_rows)
    assert all(float(r.nav) == pytest.approx(sum(leg["value"] for leg in r.composition["legs"])) for r in gap_rows)
    assert float(gap_rows[0].spy_close) == SPY[GAP[0]]
    # Re-running finds nothing missing.
    assert snapshot_gap.apply_plan(db_session, _plan(db_session)) == 0


def test_undo_removes_only_backfilled_rows(db_session):
    tenant, pf, _ = _seed(db_session)
    # A live row inside the range that the backfill must neither overwrite nor undo.
    live = _snap(models.NavSnapshot, tenant, pf, date(2026, 10, 8), [_leg("AAPL", 10, 1.0)], nav=999.0)
    db_session.add(live)
    db_session.commit()
    assert snapshot_gap.apply_plan(db_session, _plan(db_session)) == 13
    assert float(db_session.get(models.NavSnapshot, live.id).nav) == 999.0
    assert snapshot_gap.undo(db_session, start=GAP[0], end=GAP[1], dry_run=True) == 13
    assert snapshot_gap.undo(db_session, start=GAP[0], end=GAP[1], dry_run=False) == 13
    left = db_session.scalars(select(models.NavSnapshot.snap_date)).all()
    assert sorted(left) == [A_DAY, date(2026, 10, 8), B_DAY]
    assert len(db_session.scalars(select(models.AccountNavSnapshot)).all()) == 2


def test_quantity_change_inside_gap_is_skipped(db_session):
    _seed(db_session, b_legs=[_leg("AAPL", 12, AAPL[B_DAY])])
    plans = _plan(db_session)
    assert all(p.skipped == "quantity of AAPL changed inside the gap" and not p.points for p in plans)
    assert snapshot_gap.apply_plan(db_session, plans) == 0


def test_missing_close_on_a_session_inside_gap_is_skipped_not_carried(db_session):
    # No AAPL bar on 10-08 although SPY closed that day: as-of would carry 10-07 forward.
    bars = {d: c for d, c in AAPL.items() if d != date(2026, 10, 8)}
    _seed(db_session, aapl_bars=bars)
    plans = _plan(db_session)
    assert all(p.skipped == "no close for a held leg on 2026-10-08" and not p.points for p in plans)


def test_history_that_stops_before_the_gap_is_refused(db_session):
    _seed(db_session, aapl_bars={d: c for d, c in AAPL.items() if d <= date(2026, 10, 7)})
    assert all(p.skipped and not p.points for p in _plan(db_session))


def test_closing_snapshot_not_reproduced_is_skipped(db_session):
    _seed(db_session, b_nav=10 * AAPL[B_DAY] * 1.05)  # recorded NAV 5% off the re-pricing
    plans = _plan(db_session)
    assert all(p.skipped.startswith("pricing method does not reproduce") for p in plans)


def test_constant_priced_leg_without_history_is_kept_varying_one_is_refused(db_session):
    cash = [_leg("AAPL", 10, AAPL[A_DAY]), _leg("CASH-USD", 500, 1.0)]
    _seed(db_session, a_legs=cash, b_legs=[_leg("AAPL", 10, AAPL[B_DAY]), _leg("CASH-USD", 500, 1.0)])
    plans = _plan(db_session)
    assert all(p.skipped is None for p in plans)
    assert plans[0].points[GAP[0]] == pytest.approx(10 * AAPL[GAP[0]] + 500)


def test_broker_priced_leg_that_moved_is_refused(db_session):
    _seed(
        db_session,
        a_legs=[_leg("AAPL", 10, AAPL[A_DAY]), _leg("PRIVATE-X", 1, 100.0)],
        b_legs=[_leg("AAPL", 10, AAPL[B_DAY]), _leg("PRIVATE-X", 1, 104.0)],
    )
    assert all(p.skipped and not p.points for p in _plan(db_session))


def test_bond_multiplier_is_taken_from_the_recorded_leg(db_session):
    bond = models.Security(symbol="BOND1", currency="USD", asset_class="bond")
    _seed(
        db_session,
        a_legs=[_leg("AAPL", 10, AAPL[A_DAY]), _leg("BOND1", 1000, 99.0, value=990.0)],
        b_legs=[_leg("AAPL", 10, AAPL[B_DAY]), _leg("BOND1", 1000, 99.0, value=990.0)],
        extra_securities=(bond,),
    )
    db_session.flush()
    db_session.add_all([models.PriceBar(security_id=bond.id, bar_date=d, close=99.0, currency="USD")
                        for d in AAPL])
    db_session.commit()
    plans = _plan(db_session)
    assert all(p.skipped is None for p in plans)
    assert plans[0].points[GAP[0]] == pytest.approx(10 * AAPL[GAP[0]] + 990.0)


def test_cli_refuses_a_range_reaching_today(monkeypatch):
    monkeypatch.setattr(maintenance, "session_today", lambda: date(2026, 10, 12))
    assert maintenance.main(["backfill-snapshot-gap", "--from", "2026-10-06", "--through", "2026-10-12"]) == 2


def test_cli_dry_run_writes_nothing_and_prints_ids_only(db_session, session_factory, monkeypatch, capsys):
    _seed(db_session)
    monkeypatch.setattr(maintenance, "session_today", lambda: date(2026, 10, 14))
    monkeypatch.setattr(maintenance, "SessionLocal", session_factory)
    monkeypatch.setattr(maintenance, "create_all", lambda: None)
    assert maintenance.main(["backfill-snapshot-gap", "--from", "2026-10-06", "--through", "2026-10-12"]) == 0
    out = capsys.readouterr().out
    assert '"mode": "dry-run"' in out and '"would_write": 14' in out and '"written": 0' in out
    assert "IBKR" not in out and '"P"' not in out  # ids, never names
    assert len(db_session.scalars(select(models.NavSnapshot)).all()) == 2
