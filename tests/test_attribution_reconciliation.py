"""Sector attribution is reconciled to the observed returns over its window (metron-ops-I372).

The decomposition uses today's holdings at today's weights against a benchmark rebuilt
from sector ETFs. Before this file, nothing tied it to what actually happened: the page
showed an active return no other surface could produce, with no residual. Now every
computable result carries a ``reconciliation``: the portfolio's observed flow-neutralized
TWR from its NAV snapshots and the benchmark's own close return, over exactly
``[start_date, as_of]``, with the named residuals between them and the attributed figures.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.config import settings
from api.db import models
from api.services import attribution
from api.services.performance import PerfPoint
from portfolio_analytics.prices import ClosePoint
from tests.test_attribution import _bench, _closes, _full_hist, _refresh, _sectors, _seed

TODAY = date(2024, 2, 20)
FIRST = date(2024, 1, 1)  # the window anchor: the first sector-ETF bar on/after the start
LAST = date(2024, 2, 19)  # the window end: the last synthetic bar


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _compute(db_session, tenant, pid, **kwargs):
    return attribution.compute_attribution(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, do_backfill=True,
        price_source=_full_hist, sector_source=_sectors, benchmark_source=_bench, **kwargs,
    )


def _snap(session, tenant, pid, when, nav, *, flow=0.0):
    session.add(models.NavSnapshot(
        tenant_id=uuid.UUID(tenant), portfolio_id=uuid.UUID(pid), snap_date=when,
        nav=nav, cost_basis=1000.0, external_flow=flow, spy_close=None,
    ))
    session.commit()


def _spy_return(anchor=FIRST, end=LAST):
    series = _closes("SPY")
    first = [p for p in series if p.bar_date <= anchor][-1].close
    last = [p for p in series if p.bar_date <= end][-1].close
    return last / first - 1.0


def test_without_nav_snapshots_the_reconciliation_names_why(client, db_session, tenant, monkeypatch):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    a = _compute(db_session, tenant, pid)
    assert a.computable
    assert a.basis == "current_holdings"
    rec = a.reconciliation
    assert rec is not None and rec.available is False
    assert rec.reason == f"no NAV snapshot on {FIRST.isoformat()}"
    assert rec.observed_portfolio_return is None and rec.active_residual is None
    assert rec.within_tolerance is False
    # The benchmark half is still measured, over the same window, from its own closes.
    assert rec.observed_benchmark_return == pytest.approx(_spy_return())
    assert rec.benchmark_residual == pytest.approx(_spy_return() - a.benchmark_return)


def test_the_residuals_are_observed_minus_attributed_over_the_same_window(
    client, db_session, tenant, monkeypatch
):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    attributed = _compute(db_session, tenant, pid)
    # A NAV path whose TWR is the attributed holdings return plus exactly one point.
    observed = attributed.portfolio_return + 0.01
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, FIRST + timedelta(days=20), 1000.0 * (1 + observed / 2))
    _snap(db_session, tenant, pid, LAST, 1000.0 * (1 + observed))

    a = _compute(db_session, tenant, pid)
    rec = a.reconciliation
    assert rec.available is True and rec.reason is None
    assert rec.observed_portfolio_return == pytest.approx(observed)
    assert rec.portfolio_residual == pytest.approx(0.01)
    assert rec.observed_benchmark_return == pytest.approx(_spy_return())
    assert rec.observed_active_return == pytest.approx(observed - _spy_return())
    # The three residuals compose: active = portfolio − benchmark.
    assert rec.active_residual == pytest.approx(rec.portfolio_residual - rec.benchmark_residual)
    assert rec.active_residual == pytest.approx(rec.observed_active_return - a.active_return)
    assert rec.within_tolerance is (abs(rec.active_residual) <= rec.tolerance)


def test_a_deposit_inside_the_window_is_not_return(client, db_session, tenant, monkeypatch):
    """A 500 deposit on a mid-window day: the observed return is the flow-neutralized TWR
    (+2% then +1%), not the 1000 → 1530 NAV move."""
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    mid = FIRST + timedelta(days=20)
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, mid, 1520.0, flow=500.0)  # (1520 − 500) / 1000 = +2%
    _snap(db_session, tenant, pid, LAST, 1535.2)  # 1535.2 / 1520 = +1%
    rec = _compute(db_session, tenant, pid).reconciliation
    assert rec.available
    assert rec.observed_portfolio_return == pytest.approx(1.02 * 1.01 - 1.0)


def test_a_missing_end_snapshot_is_a_reason_not_a_nearby_window(client, db_session, tenant, monkeypatch):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, LAST - timedelta(days=1), 1100.0)
    rec = _compute(db_session, tenant, pid).reconciliation
    assert rec.available is False
    assert rec.reason == f"no NAV snapshot on {LAST.isoformat()}"
    assert rec.observed_portfolio_return is None


def test_snapshots_outside_the_window_do_not_enter_it(client, db_session, tenant, monkeypatch):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    _snap(db_session, tenant, pid, FIRST - timedelta(days=5), 10.0)  # would read as +9900%
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, LAST, 1050.0)
    _snap(db_session, tenant, pid, LAST + timedelta(days=3), 5000.0)
    rec = _compute(db_session, tenant, pid).reconciliation
    assert rec.observed_portfolio_return == pytest.approx(0.05)


def test_an_account_selection_reconciles_against_the_selected_accounts(
    client, db_session, tenant, monkeypatch
):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    acct_id = db_session.scalars(
        select(models.Account.id).where(models.Account.portfolio_id == uuid.UUID(pid))
    ).first()
    assert acct_id is not None
    for when, nav in ((FIRST, 500.0), (LAST, 540.0)):
        db_session.add(models.AccountNavSnapshot(
            tenant_id=uuid.UUID(tenant), portfolio_id=uuid.UUID(pid), account_id=acct_id, snap_date=when,
            nav=nav, cost_basis=500.0, external_flow=0.0,
        ))
    # A whole-portfolio series that says something else: the selection must not read it.
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, LAST, 2000.0)
    db_session.commit()
    rec = _compute(db_session, tenant, pid, account_ids={acct_id}).reconciliation
    assert rec.available
    assert rec.observed_portfolio_return == pytest.approx(0.08)


class TestReconcile:
    """``_reconcile`` alone: the tie case and the benchmark half's failure modes."""

    def _points(self, *navs, start=FIRST, step=7):
        return [PerfPoint(start + timedelta(days=step * i), nav, 0.0, None) for i, nav in enumerate(navs)]

    def test_ties_within_tolerance_when_observed_equals_attributed(self):
        pts = self._points(1000.0, 1010.0, 1030.0)
        end = pts[-1].snap_date
        spy = [ClosePoint(FIRST, 100.0), ClosePoint(end, 102.0)]
        rec = attribution._reconcile(
            pts, spy, benchmark="SPY", anchor=FIRST, end=end, portfolio_return=0.03, benchmark_return=0.02,
        )
        assert rec.available and rec.within_tolerance
        assert rec.active_residual == pytest.approx(0.0)

    def test_outside_tolerance_is_reported_not_refused(self):
        pts = self._points(1000.0, 1050.0)
        end = pts[-1].snap_date
        spy = [ClosePoint(FIRST, 100.0), ClosePoint(end, 101.0)]
        rec = attribution._reconcile(
            pts, spy, benchmark="SPY", anchor=FIRST, end=end, portfolio_return=0.03, benchmark_return=0.01,
        )
        assert rec.available and not rec.within_tolerance
        assert rec.portfolio_residual == pytest.approx(0.02)
        assert rec.benchmark_residual == pytest.approx(0.0)
        assert rec.active_residual == pytest.approx(0.02)

    def test_a_stale_benchmark_series_is_named(self):
        pts = self._points(1000.0, 1050.0)
        end = pts[-1].snap_date
        spy = [ClosePoint(FIRST, 100.0), ClosePoint(end - timedelta(days=2), 101.0)]
        rec = attribution._reconcile(
            pts, spy, benchmark="QQQ", anchor=FIRST, end=end, portfolio_return=0.03, benchmark_return=0.01,
        )
        assert rec.available is False
        assert rec.reason == "QQQ close history: stale"
        assert rec.observed_portfolio_return == pytest.approx(0.05)
        assert rec.observed_benchmark_return is None and rec.active_residual is None

    def test_a_non_positive_nav_inside_the_window_is_named(self):
        pts = self._points(1000.0, 0.0, 1050.0)
        end = pts[-1].snap_date
        spy = [ClosePoint(FIRST, 100.0), ClosePoint(end, 101.0)]
        rec = attribution._reconcile(
            pts, spy, benchmark="SPY", anchor=FIRST, end=end, portfolio_return=0.03, benchmark_return=0.01,
        )
        assert rec.available is False and rec.reason == "a non-positive NAV inside the window"
        assert rec.observed_portfolio_return is None


def test_the_api_serves_the_basis_and_the_reconciliation(client, db_session, tenant, monkeypatch):
    monkeypatch.setattr(settings, "feed_entitled", True)  # attribution is feed-gated
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    _snap(db_session, tenant, pid, FIRST, 1000.0)
    _snap(db_session, tenant, pid, LAST, 1040.0)
    real = attribution.compute_attribution

    def pinned(*args, **kwargs):
        kwargs.update(today=TODAY, price_source=_full_hist, sector_source=_sectors, benchmark_source=_bench)
        return real(*args, **kwargs)

    monkeypatch.setattr(attribution, "compute_attribution", pinned)
    body = client.post(f"/portfolios/{pid}/attribution/compute", headers={"X-Tenant-Id": tenant}).json()
    assert body["computable"] is True
    assert body["basis"] == "current_holdings"
    rec = body["reconciliation"]
    assert rec["available"] is True
    assert rec["observed_portfolio_return"] == pytest.approx(0.04)
    assert rec["tolerance"] == pytest.approx(attribution.RECONCILE_TOLERANCE)
    assert set(rec) >= {"portfolio_residual", "benchmark_residual", "active_residual", "within_tolerance"}


def test_a_selected_account_without_snapshots_leaves_no_observed_return(
    client, db_session, tenant, monkeypatch
):
    # metron-ops-I392: the aggregate's cohort is the accounts WITH rows, so a selected
    # account that has none must not silently drop out of the observed return.
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    acct_id = db_session.scalars(
        select(models.Account.id).where(models.Account.portfolio_id == uuid.UUID(pid))
    ).first()
    for when, nav in ((FIRST, 500.0), (LAST, 540.0)):
        db_session.add(models.AccountNavSnapshot(
            tenant_id=uuid.UUID(tenant), portfolio_id=uuid.UUID(pid), account_id=acct_id, snap_date=when,
            nav=nav, cost_basis=500.0, external_flow=0.0,
        ))
    db_session.commit()
    uncovered = uuid.uuid4()
    points = attribution._observed_window_points(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), {acct_id, uncovered}, FIRST, LAST)
    assert points == []
    covered = attribution._observed_window_points(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), {acct_id}, FIRST, LAST)
    assert [p.snap_date for p in covered] == [FIRST, LAST]
