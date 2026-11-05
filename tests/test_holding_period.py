"""Holding period on the Holdings rows (Brian 2026-11-04)."""
from __future__ import annotations

from datetime import date

from api.services import holding_period as hp


def test_summarize_weights_days_by_shares_and_reports_coverage():
    lots = [(10.0, date(2024, 1, 2)), (30.0, date(2026, 1, 2))]
    first, days, cov = hp.summarize(lots, 50.0, date(2026, 11, 4))
    assert first == date(2024, 1, 2)
    d1, d2 = (date(2026, 11, 4) - date(2024, 1, 2)).days, (date(2026, 11, 4) - date(2026, 1, 2)).days
    assert days == (10 * d1 + 30 * d2) / 40
    assert cov == 0.8


def test_no_lots_reads_as_unknown_not_zero():
    assert hp.summarize([], 10.0, date(2026, 11, 4)) == (None, None, None)


def test_coverage_never_exceeds_the_whole_position():
    assert hp.summarize([(12.0, date(2026, 1, 1))], 10.0, date(2026, 11, 4))[2] == 1.0
