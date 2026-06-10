"""Latest EOD close per symbol via yfinance — the personal-tier price source.

yfinance is free and needs no signup, which makes it correct for the PERSONAL
(non-commercial) tier. The public multi-tenant tier publicly *displays* prices, so it
must swap in a licensed feed (Marketstack/Databento) — done by passing a different
``source`` to ``fetch_latest_closes``, not by editing callers.

Fail-soft by symbol: a ticker yfinance can't resolve (delisted, foreign listing, a
typo) is simply omitted from the result. The caller treats an absent symbol as "no
price" and shows cost basis only — never a fabricated value.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClosePoint:
    """One symbol's most recent daily close and the session it printed."""

    bar_date: date
    close: float


# A source maps a list of symbols to their latest closes. The default is yfinance;
# tests (and the future licensed-feed tier) inject their own.
PriceSource = Callable[[list[str]], dict[str, ClosePoint]]


def fetch_latest_closes(symbols: Iterable[str], *, source: PriceSource | None = None) -> dict[str, ClosePoint]:
    """Latest available daily close per symbol. Deduped, order-insensitive.

    Returns ``{}`` for an empty input. Symbols the source can't price are omitted."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    source = source or _yfinance_latest_closes
    return source(unique)


def _yfinance_latest_closes(symbols: list[str]) -> dict[str, ClosePoint]:  # pragma: no cover - network
    """Default source: one batched yfinance download, last non-NaN close per symbol.

    Network/parse failures degrade to the symbols that did resolve (or ``{}``); they
    never raise into the caller, so a flaky fetch yields fewer prices, not an error
    page. Excluded from unit coverage — exercised live, mirrors ``snaptrade_reader``.
    """
    import pandas as pd
    import yfinance as yf

    try:
        data = yf.download(
            symbols,
            period="5d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as e:
        logger.warning("yfinance close fetch failed for %d symbols: %s", len(symbols), e)
        return {}
    if data is None or data.empty:
        return {}

    out: dict[str, ClosePoint] = {}
    single = len(symbols) == 1
    for sym in symbols:
        try:
            # Single symbol → flat columns; multi → per-symbol sub-frame under data[sym].
            frame = data if single else data[sym]
            closes = frame["Close"].dropna()
            if closes.empty:
                continue
            value = float(closes.iloc[-1])
            if value <= 0:
                continue
            bar = pd.Timestamp(closes.index[-1]).date()
            out[sym] = ClosePoint(bar_date=bar, close=value)
        except Exception:  # missing/illiquid symbol — caller falls back to cost basis
            continue
    return out
