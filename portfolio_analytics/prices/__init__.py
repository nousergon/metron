"""EOD price sourcing for market value + performance.

The engine is price-source-agnostic: ``fetch_latest_closes`` takes an injectable
``source``. The default is the **data spine** — Metron reads EOD closes + FX from
`alpha-engine-data`'s S3 artifacts and makes no direct market-data API calls.
"""

from portfolio_analytics.prices.source import (
    ClosePoint,
    HistorySource,
    PriceSource,
    fetch_close_history,
    fetch_latest_closes,
)
from portfolio_analytics.prices.symbology import fx_pair_symbol, to_yf_symbol

__all__ = [
    "ClosePoint",
    "HistorySource",
    "PriceSource",
    "fetch_close_history",
    "fetch_latest_closes",
    "fx_pair_symbol",
    "to_yf_symbol",
]
