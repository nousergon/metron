"""Tax lens over open lots — holding-period term, unrealized P&L, harvestable losses.

Per open lot (the ledger keeps each lot's open date + cost), classify short- vs
long-term by holding period, value it at the latest cached close, and flag below-cost
lots as tax-loss-harvesting candidates — using the ported ``portfolio_analytics.
domain.tax`` engine. Descriptive only, never advice.

No fabrication: a lot whose ticker has no cached price gets a null market value /
unrealized and is excluded from the unrealized totals (cost basis + term still shown).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from api.services import analytics
from api.services import prices as price_service
from portfolio_analytics.domain.tax import LONG_TERM, classify_term
from portfolio_analytics.domain.tax import harvestable_loss as compute_harvestable


@dataclass
class TaxLot:
    ticker: str
    open_date: date
    quantity: float
    cost_basis: float
    term: str
    market_value: float | None
    unrealized_gain: float | None
    harvestable_loss: float


@dataclass
class TaxSummary:
    as_of: date
    n_lots: int
    n_priced: int
    unrealized_st: float | None
    unrealized_lt: float | None
    unrealized_total: float | None
    harvestable_loss: float
    lots: list[TaxLot] = field(default_factory=list)


def tax_lots(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date) -> TaxSummary:
    """Per-lot tax view: term + unrealized P&L (at the latest cached close) + harvestable
    losses. Short-term and Unknown terms aggregate into the short-term bucket (the
    conservative assumption). Totals are None until at least one lot is priced."""
    ledger = analytics.load_ledger(session, tenant_id, portfolio_id)
    prices = price_service.latest_close_by_symbol(session, list(ledger.open_lots))

    lots: list[TaxLot] = []
    st = lt = 0.0
    harvest_total = 0.0
    any_priced = False
    for ticker, open_lots in ledger.open_lots.items():
        point = prices.get(ticker)
        for lot in open_lots:
            if lot.quantity <= 0:
                continue
            cost = lot.cost_per_share * lot.quantity
            term = classify_term((today - lot.open_date).days)
            market_value = unrealized = None
            harvestable = 0.0
            if point is not None:
                market_value = point.close * lot.quantity
                unrealized = market_value - cost
                harvestable = compute_harvestable(unrealized)
                any_priced = True
                if term == LONG_TERM:
                    lt += unrealized
                else:  # short-term + Unknown → short-term bucket
                    st += unrealized
                harvest_total += harvestable
            lots.append(
                TaxLot(
                    ticker=ticker,
                    open_date=lot.open_date,
                    quantity=lot.quantity,
                    cost_basis=cost,
                    term=term,
                    market_value=market_value,
                    unrealized_gain=unrealized,
                    harvestable_loss=harvestable,
                )
            )
    lots.sort(key=lambda x: (x.ticker, x.open_date))
    return TaxSummary(
        as_of=today,
        n_lots=len(lots),
        n_priced=sum(1 for x in lots if x.market_value is not None),
        unrealized_st=st if any_priced else None,
        unrealized_lt=lt if any_priced else None,
        unrealized_total=(st + lt) if any_priced else None,
        harvestable_loss=harvest_total,
        lots=lots,
    )
