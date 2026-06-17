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
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import account_meta, analytics, fx
from api.services import prices as price_service
from portfolio_analytics.domain.tax import LONG_TERM, classify_term
from portfolio_analytics.domain.tax import harvestable_loss as compute_harvestable


@dataclass
class TaxLot:
    ticker: str
    open_date: date
    quantity: float
    currency: str
    cost_basis: float            # native total cost basis (lot identity is native)
    term: str
    # Base-currency valuation (None when unpriced, or when the FX rate isn't cached).
    cost_basis_base: float | None
    market_value: float | None
    unrealized_gain: float | None
    harvestable_loss: float | None


@dataclass
class TaxSummary:
    as_of: date
    base_currency: str
    n_lots: int
    n_priced: int
    # Base-currency unrealized totals (None until at least one lot is priced + convertible).
    # ``unrealized_st``/``unrealized_lt``/``unrealized_total`` are the LOT-CLASSIFIED figures
    # (reconstructable from the transaction ledger → term + harvesting). They under-count when
    # a position's broker history starts mid-position (the lots can't be replayed).
    unrealized_st: float | None
    unrealized_lt: float | None
    unrealized_total: float | None
    harvestable_loss: float | None
    # The AUTHORITATIVE total unrealized for the in-scope (taxable) accounts, valued from
    # current positions (the same engine the Accounts panel uses) — reconciles to the
    # Accounts table. ``>= unrealized_total``; the difference sits in positions whose lot
    # history is incomplete (``incomplete_tickers``). None until at least one position is priced.
    unrealized_position_total: float | None = None
    n_accounts_excluded: int = 0  # tax-advantaged accounts filtered out of this view
    # Positions valued in the total but NOT lot-classifiable: their broker activity feed
    # starts mid-position, so the opening BUYs can't be replayed into datable lots. We note
    # the gap professionally rather than silently under-reporting the unrealized total.
    n_incomplete: int = 0
    incomplete_tickers: list[str] = field(default_factory=list)
    lots: list[TaxLot] = field(default_factory=list)


def tax_lots(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    taxable_only: bool = True,
    selected_account_ids: Collection[uuid.UUID] | None = None,
) -> TaxSummary:
    """Per-lot tax view: term + unrealized P&L (at the latest cached close, converted to
    the portfolio base currency) + harvestable losses. Short-term and Unknown terms
    aggregate into the short-term bucket (the conservative assumption). Base-currency
    totals are None until at least one lot is both priced AND convertible.

    ``taxable_only`` (default) restricts the lots to taxable accounts — unrealized gains
    inside an IRA/401(k)/Roth are never taxed, so harvesting/​income figures over them
    would mislead. Tax-advantaged accounts are excluded and counted.

    ``selected_account_ids`` (the user's account-panel selection) further narrows the
    view. When ``taxable_only`` is on, the selection is **intersected** with the taxable
    set — the taxable-only safety always wins, so picking a retirement account can never
    leak its lots into the Tax lens. ``n_accounts_excluded`` counts the candidate
    accounts dropped for being tax-advantaged."""
    base = analytics._base_currency(session, portfolio_id)
    selected = set(selected_account_ids) if selected_account_ids is not None else None
    # Candidate accounts = the selection if any, else every account in the portfolio.
    candidate_ids = selected if selected is not None else set(
        session.scalars(
            select(models.Account.id).where(
                models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
            )
        ).all()
    )
    account_ids: Collection[uuid.UUID] | None = selected
    n_excluded = 0
    if taxable_only:
        taxable = account_meta.taxable_account_ids(session, tenant_id, portfolio_id)
        account_ids = candidate_ids & taxable
        n_excluded = len(candidate_ids) - len(account_ids)
    ledger, incomplete = analytics.load_ledger(session, tenant_id, portfolio_id, account_ids=account_ids)
    prices = price_service.latest_close_by_symbol(session, list(ledger.open_lots))
    ccy_by_ticker = analytics._currency_by_symbol(session, list(ledger.open_lots))
    fx_rates = fx.rates_to_base(session, list(ccy_by_ticker.values()), base=base)

    lots: list[TaxLot] = []
    st = lt = 0.0
    harvest_total = 0.0
    any_priced = False
    for ticker, open_lots in ledger.open_lots.items():
        point = prices.get(ticker)
        currency = ccy_by_ticker.get(ticker, "USD")
        rate = fx_rates.get(currency)
        for lot in open_lots:
            if lot.quantity <= 0:
                continue
            cost = lot.cost_per_share * lot.quantity  # native
            cost_base = cost * rate if rate is not None else None
            term = classify_term((today - lot.open_date).days)
            market_value = unrealized = harvestable = None
            if point is not None and rate is not None:
                market_value = point.close * lot.quantity * rate  # base
                unrealized = market_value - (cost_base or 0.0)
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
                    currency=currency,
                    cost_basis=cost,
                    term=term,
                    cost_basis_base=cost_base,
                    market_value=market_value,
                    unrealized_gain=unrealized,
                    harvestable_loss=harvestable,
                )
            )
    lots.sort(key=lambda x: (x.ticker, x.open_date))

    # Reconcile to the position-level truth. The lot view above can only sum lots it could
    # replay from the transaction ledger; a position whose broker activity feed starts
    # mid-position is dropped from the ledger (flagged in ``incomplete``) yet still has a
    # correct broker-snapshot cost basis. ``valued_holdings`` over the SAME (taxable) scope
    # is exactly what the Accounts panel sums, so it is the authoritative unrealized total —
    # we surface it and note the un-classifiable remainder rather than under-reporting.
    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    priced_unreal = [h.unrealized_gain for h in held if h.unrealized_gain is not None]
    position_total = sum(priced_unreal) if priced_unreal else None
    incomplete_tickers = sorted({i.ticker for i in incomplete if i.ticker})

    return TaxSummary(
        as_of=today,
        base_currency=base,
        n_lots=len(lots),
        n_priced=sum(1 for x in lots if x.market_value is not None),
        unrealized_st=st if any_priced else None,
        unrealized_lt=lt if any_priced else None,
        unrealized_total=(st + lt) if any_priced else None,
        unrealized_position_total=position_total,
        harvestable_loss=harvest_total if any_priced else None,
        n_accounts_excluded=n_excluded,
        n_incomplete=len(incomplete),
        incomplete_tickers=incomplete_tickers,
        lots=lots,
    )
