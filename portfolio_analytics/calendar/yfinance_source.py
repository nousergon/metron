"""Next-earnings-date source seam, yfinance-backed.

Maps held tickers → their next scheduled earnings date. yfinance is free and needs no
signup (correct for the personal AND public free tier). Fail-soft per ticker: one that
yfinance can't resolve (no scheduled date, delisted, a blip) is simply absent — the
caller shows an earnings-free calendar rather than a fabricated date.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime

logger = logging.getLogger(__name__)

# A source maps symbols → each symbol's next earnings date (omitting unknowns). The
# default is yfinance; tests + any future feed inject their own.
EarningsSource = Callable[[list[str]], dict[str, date]]


def fetch_earnings_dates(symbols: Iterable[str], *, source: EarningsSource | None = None) -> dict[str, date]:
    """Next earnings date per symbol. Deduped, order-insensitive.

    Returns ``{}`` for empty input. Symbols without a resolvable upcoming date are
    omitted (never fabricated)."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    source = source or _yfinance_earnings_dates
    return source(unique)


def _yfinance_earnings_dates(symbols: list[str]) -> dict[str, date]:  # pragma: no cover - network
    """Default source: ``Ticker(sym).calendar['Earnings Date']`` per symbol.

    yfinance returns a list of candidate dates; we take the earliest. Fail-soft per
    symbol. Excluded from unit coverage; exercised live, mirroring the other sources."""
    import yfinance as yf

    out: dict[str, date] = {}
    for sym in symbols:
        try:
            cal = yf.Ticker(sym).calendar or {}
            raw = cal.get("Earnings Date")
        except Exception as e:  # network / schema
            logger.warning("yfinance earnings lookup failed for %s: %s", sym, e)
            continue
        parsed = _coerce_date(raw)
        if parsed is not None:
            out[sym] = parsed
    return out


def _coerce_date(raw) -> date | None:  # pragma: no cover - network-shape normalization
    """Normalize yfinance's earnings-date value (a date, datetime, or list thereof) to
    the earliest plain ``date``. None if there's nothing usable."""
    if raw is None:
        return None
    candidates = raw if isinstance(raw, (list, tuple)) else [raw]
    dates: list[date] = []
    for c in candidates:
        if isinstance(c, datetime):  # datetime is a date subclass — normalize to plain date
            dates.append(c.date())
        elif isinstance(c, date):
            dates.append(c)
    return min(dates) if dates else None
