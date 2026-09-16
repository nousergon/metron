"""``is_stale`` staleness helper (metron-ops-I308) — the factor-profile substrate's
weekly-plus-buffer freshness window, reused by api.services.attractiveness."""

from __future__ import annotations

from datetime import date, timedelta

from api.services import factor_profiles


def test_none_as_of_is_stale():
    assert factor_profiles.is_stale(None) is True


def test_fresh_as_of_is_not_stale():
    assert factor_profiles.is_stale(date.today()) is False


def test_as_of_at_boundary_is_not_stale():
    boundary = date.today() - timedelta(days=factor_profiles.STALE_AFTER_DAYS)
    assert factor_profiles.is_stale(boundary) is False


def test_as_of_past_boundary_is_stale():
    past = date.today() - timedelta(days=factor_profiles.STALE_AFTER_DAYS + 1)
    assert factor_profiles.is_stale(past) is True


def test_today_override_is_honored():
    as_of = date(2026, 1, 1)
    assert factor_profiles.is_stale(as_of, today=date(2026, 1, 1)) is False
    assert factor_profiles.is_stale(as_of, today=date(2026, 1, 20)) is True
