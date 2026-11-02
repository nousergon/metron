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

from datetime import date, timedelta
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


def _weekdays(start: date, end: date) -> list[date]:
    days = (start + timedelta(n) for n in range((end - start).days + 1))
    return [d for d in days if d.weekday() < 5]


def _closes(start: date, end: date, px: float) -> list[ClosePoint]:
    """A gap-free daily close series — every input observed on (or just before) each date."""
    return [ClosePoint(d, px) for d in _weekdays(start, end)]


def _rates(start: date, end: date, rate: float) -> tuple[list[date], list[float]]:
    days = _weekdays(start, end)
    return days, [rate] * len(days)


def _points(session, tenant_id, pid, *, fx_series, history=None, current_px=None):
    history = history or {
        "0700.HK": _closes(BUY, TODAY, 300.0),
        "AAPL": _closes(BUY, TODAY, 100.0),
        "SPY": _closes(BUY, TODAY, 480.0),
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
    points = _points(db_session, tenant_id, pid, fx_series={"HKD": _rates(date(2024, 3, 15), TODAY, 0.13)})
    early = [p for p in points if p.when < date(2024, 3, 15)]
    late = [p for p in points if p.when >= date(2024, 3, 15)]
    assert early and late
    for p in early:
        assert [e for e in _fx_stamps(p, "after_date") if "leg" not in e] == [
            {"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date"}
        ], f"{p.when} used a rate from after its own date without saying so"
    # The HKD buy's flow is converted by the same rule, so the BUY point stamps it too
    # (metron-ops-I389).
    buy_point = next(p for p in early if p.when == BUY)
    assert {"ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "after_date", "leg": "flow"} in (
        buy_point.estimated_inputs
    )
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
        "0700.HK": _closes(BUY, TODAY, 300.0),
        "AAPL": _closes(date(2024, 3, 1), TODAY, 100.0),  # starts late
        "SPY": _closes(BUY, TODAY, 480.0),
    }
    points = _points(
        db_session, tenant_id, pid, fx_series={"HKD": _rates(date(2023, 12, 29), TODAY, 0.128)},
        history=history, current_px={"AAPL": 150.0},
    )
    before = [p for p in points if p.when < date(2024, 3, 1)]
    after = [p for p in points if p.when >= date(2024, 3, 1)]
    assert before and after
    for p in before:
        assert {"ticker": "AAPL", "input": "price", "reason": "after_date"} in p.estimated_inputs
    for p in after:
        assert not p.estimated_inputs


def test_a_close_carried_across_a_price_gap_is_stamped_stale_and_still_valued(db_session):
    """A gap in the close history: the last close before it is dated on/before each date in
    the gap, so it is not look-ahead, but past ``_STALE_INPUT_DAYS`` it is no longer an
    observation of that date. The leg keeps its value (dropping it would understate NAV);
    the point says it is an estimate and names the close it carried."""
    tenant_id, pid = _seed(db_session)
    gap_from, gap_to = date(2024, 1, 31), date(2024, 3, 1)
    history = {
        "0700.HK": _closes(BUY, TODAY, 300.0),
        "AAPL": _closes(BUY, gap_from, 100.0) + _closes(gap_to, TODAY, 100.0),
        "SPY": _closes(BUY, TODAY, 480.0),
    }
    points = _points(
        db_session, tenant_id, pid, fx_series={"HKD": _rates(date(2023, 12, 29), TODAY, 0.128)}, history=history,
    )
    stale = {"ticker": "AAPL", "input": "price", "reason": "stale", "observed": gap_from.isoformat()}
    limit = gap_from + timedelta(days=performance._STALE_INPUT_DAYS)
    in_gap = [p for p in points if limit < p.when < gap_to]
    fresh = [p for p in points if p.when <= limit or p.when >= gap_to]
    assert in_gap and fresh
    for p in in_gap:
        assert p.estimated_inputs == [stale], f"{p.when} carried a stale close without saying so"
        assert p.nav == pytest.approx(10 * 300 * 0.128 + 1000.0), "the stale leg must stay in NAV"
    for p in fresh:
        assert not p.estimated_inputs, f"{p.when} valued every leg from a fresh as-of input"


def test_an_fx_rate_carried_across_a_gap_is_stamped_stale(db_session):
    tenant_id, pid = _seed(db_session)
    last = date(2024, 1, 15)
    fx = _rates(date(2023, 12, 29), last, 0.128)
    points = _points(db_session, tenant_id, pid, fx_series={"HKD": fx})
    limit = last + timedelta(days=performance._STALE_INPUT_DAYS)
    late = [p for p in points if p.when > limit]
    assert late
    for p in late:
        assert _fx_stamps(p, "stale") == [{
            "ticker": "0700.HK", "input": "fx", "currency": "HKD", "reason": "stale", "observed": last.isoformat(),
        }]
        assert p.nav == pytest.approx(10 * 300 * 0.128 + 1000.0)
    assert all(not p.estimated_inputs for p in points if p.when <= limit)


def test_rate_history_reports_the_date_of_the_rate_it_returns():
    hist = fx_service.RateHistory("USD", {"HKD": ([date(2024, 1, 2), date(2024, 1, 9)], [0.128, 0.129])})
    assert hist.observed_as_of("HKD", date(2024, 1, 8)) == date(2024, 1, 2)
    assert hist.observed_as_of("HKD", date(2024, 1, 9)) == date(2024, 1, 9)
    assert hist.observed_as_of("HKD", date(2024, 1, 1)) is None
    assert hist.observed_as_of("USD", date(2024, 1, 1)) == date(2024, 1, 1)


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


# ── corrected vintages, end to end ─────────────────────────────────────────


def _store_inputs(session, *, aapl_px: float = 100.0):
    """Daily closes and HKD rates in the database, as the backfill would have left them."""
    secs = {s.symbol: s for s in session.scalars(select(models.Security)).all()}
    spy = models.Security(symbol="SPY", currency="USD")
    session.add(spy)
    session.flush()
    for d in _weekdays(BUY, TODAY):
        session.add_all([
            models.PriceBar(security_id=secs["0700.HK"].id, bar_date=d, close=300.0, currency="HKD"),
            models.PriceBar(security_id=secs["AAPL"].id, bar_date=d, close=aapl_px, currency="USD"),
            models.PriceBar(security_id=spy.id, bar_date=d, close=480.0, currency="USD"),
            models.FxRate(currency="HKD", base="USD", rate_date=d, rate=0.128),
        ])
    session.commit()
    return secs["AAPL"].id


def _offline(monkeypatch):
    """Reconstruction reads only what is stored: no price or FX fetches."""
    monkeypatch.setattr(performance.price_service, "ensure_security", lambda *a, **k: None)
    monkeypatch.setattr(performance.price_service, "backfill_prices", lambda *a, **k: 0)
    monkeypatch.setattr(performance.fx_service, "backfill_fx_rates", lambda *a, **k: 0)


def test_a_corrected_close_restates_the_point_and_keeps_the_value_it_served(db_session, monkeypatch):
    """A later, corrected vintage of an input (a revised close) changes the dated valuation
    the next reconstruction writes. The value served before the correction must survive as
    a linked revision of the same snapshot row, and be reproducible from the original input."""
    _offline(monkeypatch)
    tenant_id, pid = _seed(db_session)
    aapl = _store_inputs(db_session)
    performance.reconstruct_snapshots(db_session, tenant_id, pid, today=TODAY)
    when = date(2024, 1, 31)
    live = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid, models.NavSnapshot.snap_date == when)
    ).one()
    served = float(live.nav)
    assert not live.estimated_inputs, "every input was observed as of the date: exact"
    assert _revisions(db_session, pid) == []

    bar = db_session.scalars(
        select(models.PriceBar).where(models.PriceBar.security_id == aapl, models.PriceBar.bar_date == when)
    ).one()
    bar.close = 90.0  # the vendor's corrected close for that day
    db_session.commit()
    performance.reconstruct_snapshots(db_session, tenant_id, pid, today=TODAY)

    db_session.refresh(live)
    assert float(live.nav) == pytest.approx(served - 10 * (100.0 - 90.0)), "only the corrected leg moved"
    revs = [r for r in _revisions(db_session, pid) if r.snap_date == when]
    assert [(r.revision, r.change, float(r.nav), r.snapshot_id) for r in revs] == [(1, "update", served, live.id)]
    # Re-running on unchanged inputs is idempotent: no further restatement.
    performance.reconstruct_snapshots(db_session, tenant_id, pid, today=TODAY)
    assert len([r for r in _revisions(db_session, pid) if r.snap_date == when]) == 1
    # The superseded value is reproducible from the original input vintage.
    bar.close = 100.0
    db_session.commit()
    replay = {p.when: p.nav for p in performance._reconstruct_nav_points(
        db_session, tenant_id, pid, account_ids=None, today=TODAY, backfill=False,
    )}
    assert replay[when] == pytest.approx(served)


def test_the_performance_series_carries_each_points_stamps(db_session):
    """Per-leg missingness is explicit on every served point, not only in the headline."""
    tenant_id, pid = _seed(db_session)
    stamp = [{"ticker": "AAPL", "input": "price", "reason": "stale", "observed": "2024-01-02"}]
    _write(db_session, tenant_id, pid, 1000.0, when=date(2024, 1, 2))
    _write(db_session, tenant_id, pid, 1010.0, when=date(2024, 1, 31), estimated_inputs=stamp)
    summary = performance.performance(db_session, tenant_id, pid)
    by_day = {p.snap_date: p.estimated_inputs for p in summary.points}
    assert by_day == {date(2024, 1, 2): None, date(2024, 1, 31): stamp}
