"""Next-earnings-date source seam.

Maps held tickers → their next scheduled earnings date. The DEFAULT is the **data
spine** — Metron reads earnings from `alpha-engine-data`'s S3 artifact and makes no
direct fetch (imported lazily, so importing this module needs no boto3/network).
Fail-soft per ticker: one the source can't resolve is simply absent — the caller shows
an earnings-free calendar rather than a fabricated date.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date

# A source maps symbols → each symbol's next earnings date (omitting unknowns). Default
# = data spine; tests + any alternate feed inject their own.
EarningsSource = Callable[[list[str]], dict[str, date]]


def fetch_earnings_dates(symbols: Iterable[str], *, source: EarningsSource | None = None) -> dict[str, date]:
    """Next earnings date per symbol. Deduped, order-insensitive. Returns ``{}`` for
    empty input; symbols the source can't resolve are omitted."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    if source is None:
        from portfolio_analytics.calendar.spine_source import spine_earnings_dates
        source = spine_earnings_dates
    return source(unique)
