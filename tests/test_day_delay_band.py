"""The ~95% delay band on the account Day % (Brian, 2026-10-28): the quotes are ~15 min
delayed, so the true Day % sits within ±1.96·σ_daily·√(minutes/390) of the shown one."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from api.db import models
from api.services import performance
from api.services.performance import SeriesPoint


def _points(rets: list[float]) -> list[SeriesPoint]:
    g, out = 1.0, [SeriesPoint(when=date(2026, 1, 1), g=1.0)]
    for i, r in enumerate(rets, start=1):
        g *= 1 + r
        out.append(SeriesPoint(when=date(2026, 1, 1) + timedelta(days=i), g=g))
    return out


def test_daily_vol_is_the_sample_stdev_of_daily_returns():
    rets = [0.01, -0.01] * 15
    assert performance.daily_vol(_points(rets)) == pytest.approx(0.01017, rel=1e-3)


def test_daily_vol_needs_enough_history():
    assert performance.daily_vol(_points([0.01] * 10)) is None


def test_band_scales_with_the_unseen_minutes():
    now = datetime(2026, 10, 28, 18, 45, tzinfo=UTC)
    # Snapshot 5 min old + 15 min vendor delay = 20 unseen minutes at 1% daily vol.
    band = performance.delay_band(0.01, "2026-10-28T18:40:00Z", now=now)
    assert band == pytest.approx(1.96 * 0.01 * (20 / 390) ** 0.5)
    older = performance.delay_band(0.01, "2026-10-28T18:20:00Z", now=now)
    assert older > band


def test_band_is_none_without_vol_or_snapshot_time():
    assert performance.delay_band(None, "2026-10-28T18:40:00Z") is None
    assert performance.delay_band(0.01, None) is None


def _seed_snapshots(session, rets: list[float], *, n_accounts: int = 1):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    ids = []
    for k in range(n_accounts):
        acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id=f"A{k}", currency="USD")
        session.add(acct)
        session.flush()
        ids.append(acct.id)
        nav = 1000.0
        for i, r in enumerate([0.0, *rets]):
            nav *= 1 + r
            session.add(models.AccountNavSnapshot(
                tenant_id=tenant.id, portfolio_id=pf.id, account_id=acct.id,
                snap_date=date(2026, 1, 1) + timedelta(days=i), nav=nav, cost_basis=1000, external_flow=0,
            ))
    session.commit()
    return tenant.id, pf.id, ids


def test_scope_vol_reads_the_daily_nav_snapshots(db_session):
    tid, pid, ids = _seed_snapshots(db_session, [0.01, -0.01] * 15)
    assert performance.scope_vol(db_session, tid, pid, ids) == pytest.approx(0.01017, rel=1e-2)
    assert performance.scope_vol(db_session, tid, pid, None) == pytest.approx(0.01017, rel=1e-2)


def test_scope_vol_none_when_history_is_short(db_session):
    tid, pid, ids = _seed_snapshots(db_session, [0.01] * 5)
    assert performance.scope_vol(db_session, tid, pid, ids) is None
