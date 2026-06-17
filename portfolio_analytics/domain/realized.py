"""Realized investment income, summarized by calendar year (F1 reporting).

Pure aggregation over the ledger's ``RealizedGain`` records plus per-year
dividend / interest cash totals. Answers "how much taxable investment income did
I realize this calendar year?" — the year-end income-tracking surface.

Four components, each tax-relevant in its own way:
  - **Realized capital gains**, split short- vs long-term (different rates — ST
    is ordinary-income, LT preferential), from closed lots (``RealizedGain``).
  - **Dividends** received (qualified vs non-qualified isn't distinguishable
    from the activity feed, so they're reported as one line).
  - **Interest** received.
  - **Distributions** — withdrawals from tax-DEFERRED accounts (Trad IRA / 401(k),
    incl. RMDs), which are taxable ordinary income even though the account's
    internal gains/dividends are not taxed annually ("Trad IRA is still taxable
    for retirees"). The caller decides which withdrawals qualify.

This is accounting over what the activity feed shows, not a 1099 — completeness
caveats (lots whose buy history predates the feed) are surfaced by the caller
(loaders/realized.py), not hidden here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from portfolio_analytics.domain.ledger import RealizedGain


@dataclass(frozen=True)
class YearlyIncome:
    """Realized taxable investment income for one calendar year (USD)."""

    year: int
    realized_st: float  # net short-term capital gain/loss
    realized_lt: float  # net long-term capital gain/loss
    dividends: float
    interest: float
    distributions: float = 0.0  # taxable withdrawals from tax-deferred accounts (ordinary income)

    @property
    def net_capital_gains(self) -> float:
        return self.realized_st + self.realized_lt

    @property
    def taxable_income(self) -> float:
        """Total realized taxable investment income = cap gains + dividends + interest +
        tax-deferred distributions (all ordinary income except LT cap gains)."""
        return self.net_capital_gains + self.dividends + self.interest + self.distributions


def summarize_income_by_year(
    realized: list[RealizedGain],
    dividends_by_year: dict[int, float],
    interest_by_year: dict[int, float],
    distributions_by_year: dict[int, float] | None = None,
) -> list[YearlyIncome]:
    """Aggregate realized gains + dividends + interest + distributions into per-year rows.

    ``realized`` are closed lots (each carries ``close_date``, ``gain``, and
    ``long_term``); ``dividends_by_year`` / ``interest_by_year`` /
    ``distributions_by_year`` map a calendar year to its summed cash income.
    ``distributions_by_year`` (default empty) is taxable withdrawals from tax-deferred
    accounts. Years are the union across all sources, returned **newest first** (so the
    current year leads — what the user tracks)."""
    distributions_by_year = distributions_by_year or {}
    st: dict[int, float] = defaultdict(float)
    lt: dict[int, float] = defaultdict(float)
    for r in realized:
        year = r.close_date.year
        if r.long_term:
            lt[year] += r.gain
        else:
            st[year] += r.gain

    years = set(st) | set(lt) | set(dividends_by_year) | set(interest_by_year) | set(distributions_by_year)
    rows = [
        YearlyIncome(
            year=y,
            realized_st=st.get(y, 0.0),
            realized_lt=lt.get(y, 0.0),
            dividends=dividends_by_year.get(y, 0.0),
            interest=interest_by_year.get(y, 0.0),
            distributions=distributions_by_year.get(y, 0.0),
        )
        for y in years
    ]
    rows.sort(key=lambda r: r.year, reverse=True)
    return rows
