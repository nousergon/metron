"""Tests for analytics/realized.py — per-year realized-income aggregation (pure)."""

from datetime import date

from portfolio_analytics.domain.ledger import RealizedGain
from portfolio_analytics.domain.realized import YearlyIncome, summarize_income_by_year


def _rg(open_y, close_y, gain):
    """A RealizedGain closing in `close_y`; long-term iff held > 365 days."""
    return RealizedGain(
        ticker="X",
        open_date=date(open_y, 1, 1),
        close_date=date(close_y, 6, 1),
        quantity=1,
        proceeds=100 + gain,
        cost_basis=100,
    )


def test_yearly_income_properties():
    y = YearlyIncome(year=2026, realized_st=4200, realized_lt=11800, dividends=3100, interest=420)
    assert y.net_capital_gains == 16000
    assert y.taxable_income == 16000 + 3100 + 420


def test_splits_short_vs_long_term_by_holding_period():
    realized = [_rg(2026, 2026, 500), _rg(2024, 2026, 1000)]  # ST (<1y), LT (>1y)
    rows = summarize_income_by_year(realized, {}, {})
    assert len(rows) == 1
    r = rows[0]
    assert r.year == 2026
    assert r.realized_st == 500
    assert r.realized_lt == 1000
    assert r.net_capital_gains == 1500


def test_groups_by_close_year_newest_first():
    realized = [_rg(2024, 2025, 200), _rg(2025, 2026, 300)]
    rows = summarize_income_by_year(realized, {}, {})
    assert [r.year for r in rows] == [2026, 2025]


def test_folds_dividends_and_interest_and_unions_years():
    # Dividends in a year with no realized gains still produces a row.
    rows = summarize_income_by_year([_rg(2026, 2026, 100)], {2026: 50, 2025: 75}, {2026: 10})
    by_year = {r.year: r for r in rows}
    assert set(by_year) == {2026, 2025}
    assert by_year[2026].dividends == 50
    assert by_year[2026].interest == 10
    assert by_year[2026].taxable_income == 100 + 50 + 10
    assert by_year[2025].dividends == 75
    assert by_year[2025].net_capital_gains == 0


def test_losses_are_negative_and_net_out():
    rows = summarize_income_by_year([_rg(2026, 2026, 1000), _rg(2026, 2026, -400)], {}, {})
    assert rows[0].realized_st == 600


def test_empty_inputs_give_no_rows():
    assert summarize_income_by_year([], {}, {}) == []
