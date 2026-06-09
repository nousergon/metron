"""Tax lens over unrealized positions — holding period, est. tax, harvestable loss.

Pure stdlib, data-source-agnostic. Classifies each position's gain as short- vs
long-term by holding period, estimates the tax due if it were sold today, and
flags loss positions as tax-loss-harvesting candidates. All **descriptive** — an
estimate and an information surface, never advice, and the holding period is an
*estimate* (no per-lot acquisition data; see the loader caveats).

Conventions: gains/losses and values are USD. Tax is charged on gains only
(losses incur none); an Unknown holding period is treated as short-term — the
conservative (higher-rate) assumption — and labeled as such.
"""

from __future__ import annotations

from datetime import date

LONG_TERM_DAYS = 365  # > 1 year held ⇒ long-term capital gains (US convention)

SHORT_TERM = "Short-term"
LONG_TERM = "Long-term"
UNKNOWN = "Unknown"


def holding_period_days(acq_date: str | None, asof: date) -> int | None:
    """Days from an ISO ``acq_date`` to ``asof``, or None if unparseable/missing."""
    if not acq_date:
        return None
    try:
        acq = date.fromisoformat(str(acq_date)[:10])
    except ValueError:
        return None
    return (asof - acq).days


def classify_term(days: int | None, *, long_term_days: int = LONG_TERM_DAYS) -> str:
    """``Short-term`` / ``Long-term`` / ``Unknown`` from a holding-period length."""
    if days is None:
        return UNKNOWN
    return LONG_TERM if days > long_term_days else SHORT_TERM


def tax_on_gain(gain: float, term: str, *, short_term_rate: float, long_term_rate: float) -> float:
    """Estimated tax on a position's gain (0 for a loss).

    Long-term gains use ``long_term_rate``; short-term **and Unknown** use
    ``short_term_rate`` (the conservative assumption when the term can't be dated).
    """
    if gain <= 0:
        return 0.0
    rate = long_term_rate if term == LONG_TERM else short_term_rate
    return gain * rate


def harvestable_loss(unrealized_gain: float) -> float:
    """The loss available to harvest (positive) for a below-cost position, else 0."""
    return -unrealized_gain if unrealized_gain < 0 else 0.0
