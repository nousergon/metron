"""GICS sector taxonomy + the yfinance-backed sector/benchmark source seams.

A holding's sector (for the portfolio side) and the benchmark's sector weights (for
the SPY side) are the two pieces of reference data Brinson-Fachler attribution needs
beyond prices. Both are sourced through injectable callables so the personal tier's
free yfinance default can be swapped for a licensed feed in the public tier, and so
tests inject deterministic maps instead of hitting the network.

Fail-soft by symbol, mirroring the price source: a ticker yfinance can't classify is
simply absent from the result (its market value lands in the "unclassified" coverage
gap, never silently attributed to a guessed sector).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# Canonical GICS sector label (yfinance Title-Case, as returned by ``Ticker.info``)
# → its SPDR sector ETF. The 11 GICS sectors SPY is decomposed into for the
# benchmark. SECTOR_ETF.keys() is the set of "attributable" sectors.
SECTOR_ETF: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# yfinance ``funds_data.sector_weightings`` snake_case keys → canonical sector label.
FUNDS_SECTOR_KEY: dict[str, str] = {
    "technology": "Technology",
    "financial_services": "Financial Services",
    "healthcare": "Healthcare",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "energy": "Energy",
    "industrials": "Industrials",
    "basic_materials": "Basic Materials",
    "utilities": "Utilities",
    "realestate": "Real Estate",
    "communication_services": "Communication Services",
}

# A sector source maps symbols → each symbol's GICS sector label (canonical). The
# default is yfinance; tests + the future licensed-feed tier inject their own.
SectorSource = Callable[[list[str]], dict[str, str]]
# A benchmark source returns the benchmark's GICS sector weights (canonical label →
# fraction). The default reads SPY's live weights from yfinance.
BenchmarkSource = Callable[[], dict[str, float]]


def fetch_sectors(symbols: Iterable[str], *, source: SectorSource | None = None) -> dict[str, str]:
    """GICS sector per symbol. Deduped, order-insensitive.

    Returns ``{}`` for empty input. Symbols the source can't classify are omitted
    (the caller leaves their ``sector`` NULL → counted against coverage, not guessed)."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    source = source or _yfinance_sectors
    return source(unique)


def fetch_benchmark_sector_weights(*, source: BenchmarkSource | None = None) -> dict[str, float]:
    """The benchmark's GICS sector weights (canonical label → raw fraction).

    Returns ``{}`` on any failure — the caller then can't build a benchmark and the
    attribution degrades to not-computable WITH a reason, never to a fabricated split."""
    source = source or _yfinance_spy_sector_weights
    return source()


def _yfinance_sectors(symbols: list[str]) -> dict[str, str]:  # pragma: no cover - network
    """Default sector source: ``Ticker(sym).info['sector']`` per symbol.

    Fail-soft per symbol (delisted / ETF without a single sector / network blip) — an
    unclassifiable ticker is omitted. Excluded from unit coverage; exercised live,
    mirroring the price source."""
    import yfinance as yf

    out: dict[str, str] = {}
    for sym in symbols:
        try:
            sector = (yf.Ticker(sym).info or {}).get("sector")
        except Exception as e:  # network / schema / missing info
            logger.warning("yfinance sector fetch failed for %s: %s", sym, e)
            continue
        if sector:
            out[sym] = str(sector)
    return out


def _yfinance_spy_sector_weights() -> dict[str, float]:  # pragma: no cover - network
    """Default benchmark source: SPY's live GICS sector weights, canonical-keyed.

    Returns ``{}`` on any failure. Excluded from unit coverage; exercised live."""
    import yfinance as yf

    try:
        raw = yf.Ticker("SPY").funds_data.sector_weightings or {}
    except Exception as e:  # network / schema / missing funds_data
        logger.warning("SPY sector-weight fetch failed: %s", e)
        return {}
    return {FUNDS_SECTOR_KEY[k]: float(v) for k, v in raw.items() if k in FUNDS_SECTOR_KEY}
