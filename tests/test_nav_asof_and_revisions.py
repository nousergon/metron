"""Historical NAV: as-of inputs and preserved valuations (metron-ops-I370).

Two defects, both reproduced against ``main`` before this file existed:

1. **Look-ahead / fabricated inputs.** Reconstruction valued a foreign leg at a date with
   ``rate_as_of(...) or latest(...) or 1.0``: a 2024-01 valuation with only a 2024-03 HKD rate
   silently used the later rate, and a currency with NO cached rate was converted at a
   fabricated USD parity. A leg with no close on/before the date used today's price. None of
   it was visible on the stored point, so an estimate was indistinguishable from an exact
   historical valuation.
2. **Overwrite.** Every writer of a dated snapshot (reconstruction, the provisional pass,
   repair, maintenance) replaced the row in place, so the value served for a date was lost.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.db import models
from api.services import fx as fx_service
from api.services import performance
from portfolio_analytics.prices import ClosePoint

TODAY = date(2024, 3, 20)
BUY = date(2024, 1, 2)


def _seed(session, *, foreign_qty: float = 10, usd_qty: float = 10):
    """A CSV (ledger-replayed) account holding a HKD stock and a USD stock, bought on BUY."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    hk = models.Security(symbol="0700.HK", currency="HKD")
    us = models.Security(symbol="AAPL", currency="USD")
    session.add_all([acct, hk, us])
    session.flush()
    session.add_all([
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=hk.id, txn_type="BUY",
            quantity=foreign_qty, price=300.0, amount=300.0 * foreign_qty, currency="HKD",
            trade_date=BUY, source_key="hk",
        ),
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=us.id, txn_type="BUY",
            quantity=usd_qty, price=100.0, amount=100.0 * usd_qty, currency="USD",
            trade_date=BUY, source_key="us",
        ),
    ])
    session.commit()
    return tenant.id, pf.id


def _points(session, tenant_id, pid, *, fx_series, history=None, current_px=None):
    history = history or {
        "0700.HK": [ClosePoint(BUY, 300.0), ClosePoint(TODAY, 300.0)],
        "AAPL": [ClosePoint(BUY, 100.0), ClosePoint(TODAY, 100.0)],
        "SPY": [ClosePoint(BUY, 480.0), ClosePoint(TODAY, 500.0)],
    }
    held = [
        SimpleNamespace(ticker=t, last_price=px, market_value=None, cost_basis_base=None)
        for t, px in (current_px or {}).items()
    ]
    return performance._reconstruct_nav_points(
        session, tenant_id, pid, account_ids=None, today=TODAY, backfill=False,
        history=history, snapshot_account_ids=set(), ticker_ccy={"0700.HK": "HKD", "AAPL": "USD"},
        held=held, fx_history=fx_service.RateHistory("USD", fx_series),
    )


def _fx_stamps(point, reason):
    return [e for e in point.estimated_inputs if e["input"] == "fx" and e["reason"] == reason]


# ── as-of inputs ───────────────────────────────────────────────────────────


def test_a_valuation_before_the_first_fx_rate_is_stamped_as_using_a_later_rate(db_session):
    """The audit's probe, in reconstruction: only a 2024-03-15 HKD rate exists."""
    tenant_id, pid = _seed(db_session)
    points = _points(db_session, tenant_id, pid, fx_series={"HKD": ([date(2024, 3, 15)], [0.13])})
    early = [p for p in points if p.when < date(2024, 3, 15)]
    late = [p for p in points if p.when >= date(2024, 3, 15)]
    assert early and late
    for p in early:
        assert _fx_stamps(p, "after_date") == [
            {"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date"}
        ], f"{p.when} used a rate from after its own date without saying so"
    for p in late:
        assert not p.estimated_inputs, f"{p.when} had an as-of rate and is exact"


def test_no_fx_rate_at_all_excludes_the_leg_never_converts_it_at_parity(db_session):
    """Before: ``or 1.0`` valued 3,000 HKD as $3,000. Now the leg is excluded and named,
    exactly as the live valuation excludes a holding with no cached rate."""
    tenant_id, pid = _seed(db_session)
    points = _points(db_session, tenant_id, pid, fx_series={"HKD": ([], [])})
    assert points
    for p in points:
        assert p.nav == pytest.approx(1000.0), f"{p.when}: a fabricated parity rate entered NAV ({p.nav})"
        assert _fx_stamps(p, "unavailable"), f"{p.when}: the excluded leg is not named"


def test_an_fx_rate_dated_after_the_valuation_is_never_used_when_an_asof_one_exists(db_session):
    tenant_id, pid = _seed(db_session)
    points = _points(
        db_session, tenant_id, pid,
        fx_series={"HKD": ([date(2023, 12, 29), date(2024, 3, 15)], [0.128, 0.50])},
    )
    first = min(points, key=lambda p: p.when)
    assert first.when < date(2024, 3, 15)
    # 10 × 300 HKD × 0.128 (as-of) + 10 × $100 — never the later 0.50.
    assert first.nav == pytest.approx(10 * 300 * 0.128 + 1000.0)
    assert not first.estimated_inputs


def test_a_current_price_carried_back_before_the_series_starts_is_stamped(db_session):
    tenant_id, pid = _seed(db_session)
    history = {
        "0700.HK": [ClosePoint(BUY, 300.0), ClosePoint(TODAY, 300.0)],
        "AAPL": [ClosePoint(date(2024, 3, 1), 100.0), ClosePoint(TODAY, 100.0)],  # starts late
        "SPY": [ClosePoint(BUY, 480.0), ClosePoint(TODAY, 500.0)],
    }
    points = _points(
        db_session, tenant_id, pid, fx_series={"HKD": ([date(2023, 12, 29)], [0.128])},
        history=history, current_px={"AAPL": 150.0},
    )
    before = [p for p in points if p.when < date(2024, 3, 1)]
    after = [p for p in points if p.when >= date(2024, 3, 1)]
    assert before and after
    for p in before:
        assert {"ticker": "AAPL", "input": "price", "reason": "after_date"} in p.estimated_inputs
    for p in after:
        assert not p.estimated_inputs


def test_stamps_persist_and_reach_the_headline(db_session, monkeypatch):
    """A stored estimate makes ``nav_history_estimated`` say so, even with full lot coverage."""
    tenant_id, pid = _seed(db_session)
    monkeypatch.setattr(
        performance, "_reconstruct_nav_points",
        lambda *a, **k: [performance._NavPoint(
            when=date(2024, 1, 2), nav=1390.0, cost_basis=1000.0, flow=0.0,
            estimated_inputs=[{"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date"}],
        )],
    )
    performance.reconstruct_snapshots(db_session, tenant_id, pid, today=TODAY)
    row = db_session.scalars(select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)).one()
    assert row.estimated_inputs == [{"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date"}]
    estimated, note = performance.nav_history_estimated(db_session, tenant_id, pid)
    assert estimated is True
    assert note and "after their own date" in note


# ── preserved valuations ───────────────────────────────────────────────────


def _revisions(session, pid):
    return session.scalars(
        select(models.NavSnapshotRevision)
        .where(models.NavSnapshotRevision.portfolio_id == pid)
        .order_by(models.NavSnapshotRevision.snap_date, models.NavSnapshotRevision.revision)
    ).all()


def _write(session, tenant_id, pid, nav, *, when=date(2024, 1, 31), **kw):
    performance._upsert_snapshot(session, tenant_id, pid, when, nav=nav, cost_basis=1000.0, flow=0.0,
                                 spy_close=None, **kw)
    session.commit()


def test_a_revised_valuation_keeps_the_value_it_replaced(db_session):
    tenant_id, pid = _seed(db_session)
    _write(db_session, tenant_id, pid, 1000.0)
    assert _revisions(db_session, pid) == [], "a first write supersedes nothing"

    _write(db_session, tenant_id, pid, 1100.0)  # e.g. nightly reconstruction re-upserts
    _write(db_session, tenant_id, pid, 1100.0)  # unchanged: not a restatement
    _write(db_session, tenant_id, pid, 1250.0)

    revs = _revisions(db_session, pid)
    assert [(r.revision, r.change, float(r.nav)) for r in revs] == [(1, "update", 1000.0), (2, "update", 1100.0)]
    live = db_session.scalars(select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)).one()
    assert float(live.nav) == 1250.0
    assert all(r.snapshot_id == live.id and r.kind == "portfolio" for r in revs)


def test_float_noise_below_stored_precision_is_not_a_revision(db_session):
    tenant_id, pid = _seed(db_session)
    _write(db_session, tenant_id, pid, 1234.567890123456)
    _write(db_session, tenant_id, pid, 1234.5678901234561)
    assert _revisions(db_session, pid) == []


def test_a_restamp_alone_is_not_a_restatement(db_session):
    tenant_id, pid = _seed(db_session)
    _write(db_session, tenant_id, pid, 1000.0)
    _write(db_session, tenant_id, pid, 1000.0,
           estimated_inputs=[{"ticker": "X", "input": "price", "reason": "after_date"}])
    assert _revisions(db_session, pid) == []


def test_a_deleted_valuation_is_preserved(db_session):
    """`repair_nav_snapshots` deletes outliers; what was served must survive it."""
    tenant_id, pid = _seed(db_session)
    _write(db_session, tenant_id, pid, 1000.0)
    row = db_session.scalars(select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)).one()
    snapshot_id = row.id
    db_session.delete(row)
    db_session.commit()
    (rev,) = _revisions(db_session, pid)
    assert (rev.change, float(rev.nav), rev.snapshot_id, rev.revision) == ("delete", 1000.0, snapshot_id, 1)


def test_an_expired_row_rewritten_after_commit_still_records_its_prior_value(db_session):
    """After a commit every attribute is expired; setting one must still capture the old value."""
    tenant_id, pid = _seed(db_session)
    _write(db_session, tenant_id, pid, 1000.0)
    row = db_session.scalars(select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid)).one()
    db_session.commit()  # expires `row`
    row.nav = 900.0
    db_session.commit()
    assert [float(r.nav) for r in _revisions(db_session, pid)] == [1000.0]


def test_account_grain_valuations_are_preserved_too(db_session):
    tenant_id, pid = _seed(db_session)
    acct_id = db_session.scalars(select(models.Account.id).where(models.Account.portfolio_id == pid)).one()
    for nav in (500.0, 650.0):
        performance._upsert_account_snapshot(
            db_session, tenant_id, pid, acct_id, date(2024, 1, 31), nav=nav, cost_basis=0.0, flow=0.0, spy_close=None,
        )
        db_session.commit()
    (rev,) = _revisions(db_session, pid)
    assert (rev.kind, rev.account_id, float(rev.nav), rev.revision) == ("account", acct_id, 500.0, 1)
