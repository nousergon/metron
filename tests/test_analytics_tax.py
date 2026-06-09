"""Tests for analytics/tax.py (pure tax-lens math)."""

from datetime import date

import pytest

from portfolio_analytics.domain import tax


class TestHoldingPeriod:
    def test_days_between(self):
        assert tax.holding_period_days("2024-01-01", date(2025, 1, 1)) == 366  # 2024 leap year
        assert tax.holding_period_days("2024-06-01T00:00:00", date(2024, 6, 11)) == 10  # tolerates time suffix

    def test_none_and_unparseable(self):
        assert tax.holding_period_days(None, date(2025, 1, 1)) is None
        assert tax.holding_period_days("not-a-date", date(2025, 1, 1)) is None


class TestClassifyTerm:
    def test_boundaries(self):
        assert tax.classify_term(None) == tax.UNKNOWN
        assert tax.classify_term(365) == tax.SHORT_TERM  # exactly 1y is NOT yet long-term
        assert tax.classify_term(366) == tax.LONG_TERM


class TestTaxOnGain:
    def test_long_vs_short_rate(self):
        assert tax.tax_on_gain(1000, tax.LONG_TERM, short_term_rate=0.35, long_term_rate=0.15) == pytest.approx(150)
        assert tax.tax_on_gain(1000, tax.SHORT_TERM, short_term_rate=0.35, long_term_rate=0.15) == pytest.approx(350)

    def test_unknown_uses_short_rate_conservatively(self):
        assert tax.tax_on_gain(1000, tax.UNKNOWN, short_term_rate=0.35, long_term_rate=0.15) == pytest.approx(350)

    def test_loss_incurs_no_tax(self):
        assert tax.tax_on_gain(-500, tax.SHORT_TERM, short_term_rate=0.35, long_term_rate=0.15) == 0.0


class TestHarvestableLoss:
    def test_loss_is_positive_magnitude(self):
        assert tax.harvestable_loss(-1200) == 1200
        assert tax.harvestable_loss(500) == 0.0
        assert tax.harvestable_loss(0) == 0.0
