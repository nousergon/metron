"""The ~95% delay band on the account Day % (Brian, 2026-10-28): the quotes are ~15 min
delayed, so the true Day % sits within ±1.96·σ_daily·√(minutes/390) of the shown one."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

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
