"""Pure portfolio math: ledger, realized income, tax lots, stress.

Built on the published quant core in ``alpha-engine-lib[quant]`` (factor risk,
attribution, returns, VaR/CVaR, riskstats), which is imported directly rather
than duplicated here.
"""

from portfolio_analytics.domain.ledger import (
    Ledger,
    Lot,
    RealizedGain,
    Transaction,
    TxnType,
    build_ledger,
    external_cash_flows,
)
from portfolio_analytics.domain.realized import YearlyIncome, summarize_income_by_year
from portfolio_analytics.domain.stress import (
    FactorShock,
    HistoricalScenario,
    factor_shock_impact,
    historical_scenario_impact,
)
from portfolio_analytics.domain.tax import (
    classify_term,
    harvestable_loss,
    holding_period_days,
    tax_on_gain,
)

__all__ = [
    "Ledger",
    "Lot",
    "RealizedGain",
    "Transaction",
    "TxnType",
    "build_ledger",
    "external_cash_flows",
    "YearlyIncome",
    "summarize_income_by_year",
    "FactorShock",
    "HistoricalScenario",
    "factor_shock_impact",
    "historical_scenario_impact",
    "classify_term",
    "harvestable_loss",
    "holding_period_days",
    "tax_on_gain",
]
