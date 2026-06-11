"""GICS sector taxonomy + the sector/benchmark source seams.

A holding's sector (portfolio side) and the benchmark's sector weights (SPY side) are
the reference data Brinson-Fachler attribution needs beyond prices. Both are sourced
through injectable callables (tests inject deterministic maps). The DEFAULT is the
**data spine** — Metron reads sectors from `alpha-engine-data`'s S3 artifact and makes
no direct classification fetch (imported lazily, so importing this module needs no
boto3/network).

Fail-soft by symbol, mirroring the price source: a symbol the source can't classify is
absent from the result (its market value lands in the "unclassified" coverage gap,
never silently attributed to a guessed sector).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# Canonical GICS sector label (yfinance Title-Case, as returned by ``Ticker.info``)
# → its SPDR sector ETF. The 11 GICS sectors SPY is decomposed into for the benchmark.
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
# (Kept for parity with the producer, which canonicalizes before publishing.)
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

# A sector source maps symbols → each symbol's canonical GICS label. Default = data spine.
SectorSource = Callable[[list[str]], dict[str, str]]
# A benchmark source returns the benchmark's GICS sector weights (canonical → fraction).
BenchmarkSource = Callable[[], dict[str, float]]


def fetch_sectors(symbols: Iterable[str], *, source: SectorSource | None = None) -> dict[str, str]:
    """GICS sector per symbol. Deduped, order-insensitive.

    Returns ``{}`` for empty input. Symbols the source can't classify are omitted
    (the caller leaves their ``sector`` NULL → counted against coverage, not guessed)."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    if source is None:
        from portfolio_analytics.sectors.spine_source import spine_sectors
        source = spine_sectors
    return source(unique)


def fetch_benchmark_sector_weights(*, source: BenchmarkSource | None = None) -> dict[str, float]:
    """The benchmark's GICS sector weights (canonical label → raw fraction).

    Returns ``{}`` on any failure — the caller then can't build a benchmark and the
    attribution degrades to not-computable WITH a reason, never to a fabricated split."""
    if source is None:
        from portfolio_analytics.sectors.spine_source import spine_benchmark_sector_weights
        source = spine_benchmark_sector_weights
    return source()
