"""Realized investment income from the SnapTrade activity feed, by calendar year.

Replays the activity history into the FIFO ledger to recover **realized capital
gains** (closed lots, short/long-term), and sums **dividends** and **interest**
cash activities per year. The result feeds the Tax page's year-end income tracker.

Honest completeness model — surfaced, never hidden:
  - Realized gains are reconstructed via FIFO from the activity window. If a
    position was sold but its opening BUY predates the feed, the cost basis can't
    be reconstructed and ``build_ledger`` raises — that ``(account, ticker)``
    group is skipped and its ticker recorded in ``incomplete`` so the UI can warn
    that the realized total may understate. This is a planning estimate, not a 1099.
  - Account scope: pass ``account_numbers`` to restrict to taxable accounts
    (realized gains in tax-advantaged accounts aren't taxable income anyway).
"""

from __future__ import annotations

import logging

from portfolio_analytics.broker_io.transactions import _f, _parse_date, group_transactions_by_account_ticker
from portfolio_analytics.domain.ledger import build_ledger
from portfolio_analytics.domain.realized import YearlyIncome, summarize_income_by_year

logger = logging.getLogger(__name__)

# Activity `type` (case-insensitive) → income bucket. These are cash-income
# events read straight off the feed (the ledger folds them into a running scalar,
# losing the date we need to bucket by year), so we sum them here directly.
_DIVIDEND_TYPES = {"DIVIDEND"}
_INTEREST_TYPES = {"INTEREST"}


def _filter_by_account(activities: list[dict], account_numbers: list[str] | None) -> list[dict]:
    if account_numbers is None:
        return activities
    wanted = set(account_numbers)
    return [a for a in activities if a.get("account_number", "") in wanted]


def _income_by_year(activities: list[dict], types: set[str]) -> dict[int, float]:
    """Sum ``amount`` of activities whose type is in ``types``, grouped by year."""
    out: dict[int, float] = {}
    for a in activities:
        if str(a.get("type", "")).upper().strip() not in types:
            continue
        when = _parse_date(a.get("trade_date") or a.get("settlement_date"))
        if when is None:
            continue
        out[when.year] = out.get(when.year, 0.0) + _f(a.get("amount"))
    return out


def _realized_gains(activities: list[dict]) -> tuple[list, set[str]]:
    """Replay per-(account, ticker) and collect closed lots + incomplete tickers.

    Returns ``(realized, incomplete)`` — ``realized`` is the flat list of
    ``RealizedGain`` across all replayable groups; ``incomplete`` is the set of
    tickers where at least one group's history couldn't be replayed (a SELL
    exceeding reconstructable BUYs), so its realized gains are missing/partial.
    """
    realized: list = []
    incomplete: set[str] = set()
    for (_acct, ticker), group in group_transactions_by_account_ticker(activities).items():
        try:
            ledger = build_ledger(group)
        except ValueError as e:
            # History starts mid-position — can't price the disposal. Flag the
            # ticker (recording surface for the dropped gains) rather than guess.
            logger.debug("Incomplete history for %s — realized gains skipped: %s", ticker, e)
            incomplete.add(ticker)
            continue
        realized.extend(ledger.realized)
    return realized, incomplete


def build_realized_income(
    activities: list[dict],
    *,
    account_numbers: list[str] | None = None,
    extra_realized: list | None = None,
) -> dict | None:
    """Realized capital gains + dividends + interest, summarized by calendar year.

    ``activities`` is the raw SnapTrade activity feed (each tagged with
    ``account_number``); ``account_numbers`` restricts to a subset (None = all).
    ``extra_realized`` is an optional list of ``RealizedGain`` from an authoritative
    out-of-feed source (the IBKR Flex Query — IBKR trades aren't in the SnapTrade
    feed), already account-filtered by the caller; these merge with the
    FIFO-reconstructed lots and are NOT subject to the ``incomplete`` understatement
    warning (IBKR carries full history).
    Returns a dict with:
      - ``years``: list[YearlyIncome], newest first
      - ``detail``: list of per-closed-lot dicts (ticker, dates, proceeds, basis,
        gain, term) for a year drill-down, newest close first
      - ``incomplete``: sorted tickers whose realized gains may be understated
    Returns None only when there are no activities AND no extra realized lots.
    """
    extra_realized = extra_realized or []
    if not activities and not extra_realized:
        return None
    scoped = _filter_by_account(activities, account_numbers)

    realized, incomplete = _realized_gains(scoped)
    realized = realized + extra_realized
    dividends = _income_by_year(scoped, _DIVIDEND_TYPES)
    interest = _income_by_year(scoped, _INTEREST_TYPES)
    years: list[YearlyIncome] = summarize_income_by_year(realized, dividends, interest)

    detail = [
        {
            "year": r.close_date.year,
            "ticker": r.ticker,
            "acquired": r.open_date,
            "sold": r.close_date,
            "quantity": r.quantity,
            "proceeds": r.proceeds,
            "cost_basis": r.cost_basis,
            "gain": r.gain,
            "term": "Long-term" if r.long_term else "Short-term",
        }
        for r in sorted(realized, key=lambda r: r.close_date, reverse=True)
    ]

    return {"years": years, "detail": detail, "incomplete": sorted(incomplete)}
