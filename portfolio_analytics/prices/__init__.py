"""EOD price sourcing for market value + performance.

The engine is price-source-agnostic: ``fetch_latest_closes`` takes an injectable
``source`` so the default (yfinance, free, personal-tier) can be swapped for a
licensed feed in the public multi-tenant tier without touching callers.
"""

from portfolio_analytics.prices.yfinance_source import ClosePoint, PriceSource, fetch_latest_closes

__all__ = ["ClosePoint", "PriceSource", "fetch_latest_closes"]
