"""GICS sector reference data + sourcing for Brinson sector attribution.

The canonical GICS sector taxonomy (yfinance Title-Case labels, their SPDR sector
ETFs, and the ``funds_data.sector_weightings`` snake_case → canonical mapping) plus
the two injectable source seams the attribution needs:

  - ``fetch_sectors`` — each holding's GICS sector (yfinance ``.info['sector']``);
  - ``fetch_benchmark_sector_weights`` — the benchmark's (SPY) live GICS sector
    weights (yfinance ``funds_data.sector_weightings``).

Both default to yfinance (free, personal-tier). The public multi-tenant tier swaps
in a licensed classification feed by passing a different ``source`` — never by
editing callers — exactly like the price source seam.
"""

from portfolio_analytics.sectors.source import (
    FUNDS_SECTOR_KEY,
    SECTOR_ETF,
    BenchmarkSource,
    CountrySource,
    SectorSource,
    fetch_benchmark_sector_weights,
    fetch_countries,
    fetch_sectors,
)

__all__ = [
    "FUNDS_SECTOR_KEY",
    "SECTOR_ETF",
    "BenchmarkSource",
    "CountrySource",
    "SectorSource",
    "fetch_benchmark_sector_weights",
    "fetch_countries",
    "fetch_sectors",
]
