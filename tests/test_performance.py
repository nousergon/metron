"""NAV snapshots + performance metrics (C2-6b).

Performance is forward-recorded: each price refresh snapshots that day's NAV
(idempotent per day), and TWR / cumulative return derive from the accumulated series
via alpha_engine_lib.quant.returns. The network is never hit — the price source is
monkeypatched. Invariants: a snapshot is recorded only when NAV is valuable (never
fabricated); metrics are null until ≥2 snapshots; cash flows are neutralized in TWR.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from portfolio_analytics.prices import ClosePoint

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n"  # 10 sh, cost 1000

_AAPL = ClosePoint(bar_date=date(2024, 6, 3), close=150.0)  # MV 1500
_SPY = ClosePoint(bar_date=date(2024, 6, 3), close=500.0)


def _price_src(symbols, *, source=None):
    return {s: _AAPL for s in symbols if s == "AAPL"}


def _spy_src(symbols, *, source=None):
    return {"SPY": _SPY} if "SPY" in symbols else {}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


class TestSnapshotOnRefresh:
    def test_refresh_records_snapshot(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
        pid = _seed(client, tenant)

        body = client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant)).json()
        assert body["snapshot_recorded"] is True

        from api.db import models

        snap = db_session.scalars(select_navsnapshots(pid)).first()
        assert float(snap.nav) == 1500.0
        assert float(snap.cost_basis) == 1000.0
        assert float(snap.spy_close) == 500.0
        del models

    def test_no_snapshot_when_unpriceable(self, client, tenant, monkeypatch):
        # Source prices nothing → no NAV to record, snapshot_recorded False (no fabrication).
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", lambda s, *, source=None: {})
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
        pid = _seed(client, tenant)
        body = client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant)).json()
        assert body["snapshot_recorded"] is False

    def test_snapshot_idempotent_per_day(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
        pid = _seed(client, tenant)
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        assert len(db_session.scalars(select_navsnapshots(pid)).all()) == 1


class TestPerformanceMetrics:
    def test_empty_until_two_snapshots(self, client, tenant):
        pid = _seed(client, tenant)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["n_snapshots"] == 0 and p["twr"] is None and p["cumulative_return"] is None

    def test_twr_and_cumulative_over_series(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 31), nav=1100.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["n_snapshots"] == 2
        assert p["days"] == 30
        assert p["cumulative_return"] == pytest.approx(0.10)  # (1100-0)/1000 - 1
        assert p["twr"] == pytest.approx(0.10)                # 1100/1000 - 1, no flows
        assert p["annualized_twr"] == pytest.approx(1.10 ** (365 / 30) - 1)

    def test_twr_neutralizes_a_contribution(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        # Start 1000; a +500 deposit lands with day-2's snapshot, NAV 1600 → the 100 of
        # growth (1500→1600 on invested 1500... ) must not read as a 60% gain.
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 31), nav=1600.0, external_flow=500.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # Pre-flow value on d2 = 1600 − 500 = 1100; TWR sub-period = 1100/1000 − 1 = 0.10
        # (the 500 deposit is neutralized, NOT read as a 60% gain).
        assert p["twr"] == pytest.approx(0.10)
        # cumulative is flow-adjusted: (1600 − 500)/1000 − 1 = 0.10.
        assert p["cumulative_return"] == pytest.approx(0.10)
        assert p["net_contributions"] == pytest.approx(500.0)


class TestRiskAndAlpha:
    def test_alpha_and_risk_metrics(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0, spy_close=400.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0, spy_close=420.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 3, 1), nav=1050.0, spy_close=410.0)  # a dip → variance + dd
        _insert_snapshot(db_session, tenant, pid, date(2024, 4, 1), nav=1200.0, spy_close=440.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["spy_return"] == pytest.approx(440 / 400 - 1)
        assert p["twr"] == pytest.approx(0.20)  # (1.1)(1050/1100)(1200/1050) − 1
        assert p["alpha"] == pytest.approx(p["twr"] - p["spy_return"])
        assert p["max_drawdown"] == pytest.approx(1050 / 1100 - 1)  # peak 1.10 → trough 1.05
        assert p["volatility"] is not None and p["volatility"] > 0
        assert p["sharpe"] is not None and p["sortino"] is not None

    def test_alpha_without_enough_history_for_risk(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0, spy_close=400.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0, spy_close=440.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # 2 snapshots → alpha computable, but vol/Sharpe need ≥2 returns (≥3 snapshots).
        assert p["alpha"] == pytest.approx(p["twr"] - p["spy_return"])
        assert p["volatility"] is None and p["sharpe"] is None

    def test_drawdown_is_flow_neutralized(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0)
        # Withdrew 500 with no investment loss: NAV 1100 → 600, external_flow −500.
        _insert_snapshot(db_session, tenant, pid, date(2024, 3, 1), nav=600.0, external_flow=-500.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # Flow-neutralized d3 return = (600 − (−500))/1100 − 1 = 0 → NOT a 45% drawdown.
        assert p["max_drawdown"] == pytest.approx(0.0)


class TestShortWindowAnnualizationGuard:
    """A sub-month window must NOT annualize: (1+twr)^(365/days) blows up for tiny `days`
    (and annualized vol/Sharpe from a few same-week returns is just as misleading), so
    the ANNUALIZED figures stay None while the window-agnostic ones (cumulative, TWR,
    max drawdown) still show. (metron-ops#44)"""

    def test_few_day_window_suppresses_annualized_metrics(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        # 6-day span, 3 returns, with a dip so a drawdown exists.
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 3), nav=1010.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 5), nav=1005.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 7), nav=1020.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["days"] == 6
        # Window-agnostic figures still shown (and sane — NOT extrapolated to a year).
        assert p["twr"] is not None
        assert p["cumulative_return"] == pytest.approx(0.02)  # 1020/1000 − 1
        assert p["max_drawdown"] == pytest.approx(1005 / 1010 - 1)  # peak 1010 → trough 1005
        # Annualized figures suppressed — no absurd extrapolation from 6 days.
        assert p["annualized_twr"] is None
        assert p["volatility"] is None
        assert p["sharpe"] is None
        assert p["sortino"] is None

    def test_29_day_window_suppresses_but_30_day_annualizes(self, client, db_session, tenant):
        # 29 days → just under the floor → no annualized TWR.
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 30), nav=1100.0)  # 29 days
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["days"] == 29
        assert p["twr"] == pytest.approx(0.10)
        assert p["annualized_twr"] is None

        # 30 days → at the floor → annualized TWR computed (matches the existing
        # test_twr_and_cumulative_over_series boundary).
        pid2 = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid2, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid2, date(2024, 1, 31), nav=1100.0)  # 30 days
        p2 = client.get(f"/portfolios/{pid2}/performance", headers=_hdr(tenant)).json()
        assert p2["days"] == 30
        assert p2["annualized_twr"] == pytest.approx(1.10 ** (365 / 30) - 1)


# --- helpers ---------------------------------------------------------------


def select_navsnapshots(portfolio_id):
    from sqlalchemy import select

    from api.db import models

    return select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == uuid.UUID(portfolio_id))


def _insert_snapshot(session, tenant, portfolio_id, when, *, nav, external_flow=0.0, spy_close=None):
    from api.db import models

    session.add(
        models.NavSnapshot(
            tenant_id=uuid.UUID(tenant),
            portfolio_id=uuid.UUID(portfolio_id),
            snap_date=when,
            nav=nav,
            cost_basis=1000.0,
            external_flow=external_flow,
            spy_close=spy_close,
        )
    )
    session.commit()


def test_rolling_risk_matches_full_window_when_history_shorter_than_window():
    """The rolling basket (metron-ops#67) is expanding-then-rolling: with fewer returns
    than the window, the last rolling point uses the full series, so it must equal the
    headline (full-window) metrics. Dates strictly increase; too-short history is empty."""
    from datetime import timedelta

    from api.services import performance as perf_svc

    start = date(2024, 1, 1)
    v = 1000.0
    navs = [v]
    for i in range(1, 50):  # 49 returns < _ROLLING_WINDOW (63) → expanding
        v *= 1.015 if i % 2 == 0 else 0.995  # both up and down days (downside for Sortino)
        navs.append(v)
    points = [
        perf_svc.PerfPoint(snap_date=start + timedelta(days=i), nav=navs[i], external_flow=0.0, spy_close=None)
        for i in range(len(navs))
    ]
    summary = perf_svc.PerformanceSummary(n_snapshots=len(points), points=points)
    summary.days = (points[-1].snap_date - points[0].snap_date).days
    perf_svc._apply_risk_and_alpha(summary, points)

    rolling = perf_svc._rolling_risk(points)
    assert len(rolling) >= 2
    assert all(rolling[k].snap_date < rolling[k + 1].snap_date for k in range(len(rolling) - 1))
    assert rolling[-1].snap_date == points[-1].snap_date
    assert rolling[-1].sharpe == pytest.approx(summary.sharpe)
    assert rolling[-1].sortino == pytest.approx(summary.sortino)
    assert rolling[-1].volatility == pytest.approx(summary.volatility)
    assert rolling[-1].max_drawdown == pytest.approx(summary.max_drawdown)
    # Too little history → no series.
    assert perf_svc._rolling_risk(points[:15]) == []


class TestNavJumpGuard:
    """metron-ops#74 — a NAV that jumps >3x / <1/3x vs the recent baseline (net of flow)
    is a data error (a sync racing the refresh), flagged and never persisted."""

    def test_implausible_nav_logic(self):
        from api.services import performance as perf

        base = [1000.0] * 5
        assert perf._implausible_nav(1100.0, base, 0.0) is False  # +10% — fine
        assert perf._implausible_nav(12000.0, base, 0.0) is True  # 12x — garbage
        assert perf._implausible_nav(200.0, base, 0.0) is True    # <1/3x — garbage
        assert perf._implausible_nav(6000.0, [1000.0], 5000.0) is False  # a $5k deposit explains it
        assert perf._implausible_nav(5000.0, [], 0.0) is False    # no baseline (first snapshots)
        # A previously-undercounted run recovering (375k → 835k ≈ 2.2x) is ALLOWED.
        assert perf._implausible_nav(835000.0, [375000.0] * 5, 0.0) is False

    def test_record_snapshot_skips_implausible(self, client, db_session, tenant, monkeypatch):
        from api.services import performance as perf
        from portfolio_analytics.prices import ClosePoint

        pid = _seed(client, tenant)
        # A clean ~1000 baseline.
        for d in range(1, 6):
            _insert_snapshot(db_session, tenant, pid, date(2024, 1, d), nav=1000.0)
        before = len(db_session.scalars(select_navsnapshots(pid)).all())

        # Force valued_holdings to report a 12x-inflated portfolio (the sync-race symptom).
        class _H:
            market_value = 12000.0
            cost_basis_base = 1000.0
        monkeypatch.setattr(perf.analytics, "valued_holdings", lambda *a, **k: [_H()])
        monkeypatch.setattr(perf, "fetch_latest_closes", lambda s, *, source=None: {"SPY": ClosePoint(close=500.0, bar_date=date(2024, 1, 6))})

        row = perf.record_snapshot(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 1, 6))
        assert row is None  # suspect → not persisted
        assert len(db_session.scalars(select_navsnapshots(pid)).all()) == before  # no new row


class TestRepairNavSnapshots:
    """metron-ops#74 — repair drops the outlier rows a race already persisted, idempotently."""

    def test_removes_spike_and_is_idempotent(self, db_session, tenant, client):
        from api.services import performance as perf

        pid = _seed(client, tenant)
        for d in range(1, 6):
            _insert_snapshot(db_session, tenant, pid, date(2024, 1, d), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 6), nav=12000.0)  # the 12x spike
        for d in range(7, 11):
            _insert_snapshot(db_session, tenant, pid, date(2024, 1, d), nav=1000.0)

        res = perf.repair_nav_snapshots(db_session, uuid.UUID(tenant), uuid.UUID(pid))
        assert res["count"] == 1 and res["removed"][0][0] == "2024-01-06"
        navs = [float(r.nav) for r in db_session.scalars(select_navsnapshots(pid)).all()]
        assert max(navs) == 1000.0  # spike gone

        # Idempotent — a clean series removes nothing.
        assert perf.repair_nav_snapshots(db_session, uuid.UUID(tenant), uuid.UUID(pid))["count"] == 0
