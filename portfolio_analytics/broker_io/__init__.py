"""Raw broker ingestion + parsing adapters.

IBKR Flex XML, SnapTrade activities, transaction tranching, and realized-income
assembly. These are the only modules in the engine that perform network I/O.
"""

from portfolio_analytics.broker_io.flex_xml import (
    IbkrFlexError,
    fetch_flex_xml,
    get_realized_lots,
    load_flex_files,
    parse_realized_lots,
)
from portfolio_analytics.broker_io.realized_income import build_realized_income
from portfolio_analytics.broker_io.transactions import (
    TrancheSet,
    activities_to_transactions,
    activities_to_transactions_aligned,
    group_transactions_by_account_ticker,
    reconstruct_tranches,
)

__all__ = [
    "IbkrFlexError",
    "fetch_flex_xml",
    "get_realized_lots",
    "load_flex_files",
    "parse_realized_lots",
    "build_realized_income",
    "TrancheSet",
    "activities_to_transactions",
    "activities_to_transactions_aligned",
    "group_transactions_by_account_ticker",
    "reconstruct_tranches",
]
