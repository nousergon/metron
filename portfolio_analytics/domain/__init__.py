"""Pure portfolio math: ledger, realized income, tax lots, stress.

Built on the published quant core in ``alpha-engine-lib[quant]`` (factor risk,
attribution, returns, VaR/CVaR, riskstats), which is imported directly rather
than duplicated here.
"""

from portfolio_analytics.domain.diagnostics import (
    ConcentrationMetrics,
    DiagnosticsPosition,
    DiagnosticsResult,
    GeoRow,
    SectorRow,
    StatedTargets,
    TargetDriftRow,
    compute_diagnostics,
    evaluate_target_drift,
)
from portfolio_analytics.domain.ledger import (
    PURCHASE_TYPES,
    Ledger,
    Lot,
    RealizedGain,
    Transaction,
    TxnType,
    build_ledger,
    external_cash_flows,
    relieve_lots,
)
from portfolio_analytics.domain.realized import YearlyIncome, summarize_income_by_year
from portfolio_analytics.domain.stress import (
    FactorShock,
    HistoricalScenario,
    factor_shock_impact,
    historical_scenario_impact,
)
from portfolio_analytics.domain.tax import (
    HypotheticalSale,
    LotMethod,
    LotPick,
    SaleDelta,
    SoldLot,
    TaxRates,
    classify_term,
    harvestable_loss,
    holding_period_days,
    hypothetical_sale,
    net_capital_gains,
    sale_delta,
    tax_on_gain,
)

__all__ = [
    "ConcentrationMetrics",
    "DiagnosticsPosition",
    "DiagnosticsResult",
    "GeoRow",
    "SectorRow",
    "StatedTargets",
    "TargetDriftRow",
    "compute_diagnostics",
    "evaluate_target_drift",
    "Ledger",
    "PURCHASE_TYPES",
    "Lot",
    "RealizedGain",
    "Transaction",
    "TxnType",
    "build_ledger",
    "external_cash_flows",
    "relieve_lots",
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
    "HypotheticalSale",
    "LotMethod",
    "LotPick",
    "SaleDelta",
    "SoldLot",
    "TaxRates",
    "hypothetical_sale",
    "net_capital_gains",
    "sale_delta",
]
