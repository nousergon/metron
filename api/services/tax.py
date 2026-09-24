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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import account_meta, analytics, fx
from api.services import prices as price_service
from portfolio_analytics.domain.tax import (
    LONG_TERM,
    HypotheticalSale,
    LotMethod,
    LotPick,
    SaleDelta,
    TaxRates,
    classify_term,
    hypothetical_sale,
    sale_delta,
)
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


def _tax_scope(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    taxable_only: bool,
    selected_account_ids: Collection[uuid.UUID] | None,
) -> tuple[Collection[uuid.UUID] | None, int]:
    """The account scope of a tax view and how many candidate accounts it dropped for
    being tax-advantaged. The selection (else every account) is intersected with the
    taxable set when ``taxable_only`` — the taxable-only safety always wins."""
    selected = set(selected_account_ids) if selected_account_ids is not None else None
    if not taxable_only:
        return selected, 0
    # Candidate accounts = the selection if any, else every account in the portfolio.
    candidate_ids = selected if selected is not None else set(
        session.scalars(
            select(models.Account.id).where(
                models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
            )
        ).all()
    )
    taxable = account_meta.taxable_account_ids(session, tenant_id, portfolio_id)
    account_ids = candidate_ids & taxable
    return account_ids, len(candidate_ids) - len(account_ids)


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
    account_ids, n_excluded = _tax_scope(
        session, tenant_id, portfolio_id, taxable_only=taxable_only, selected_account_ids=selected_account_ids
    )
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


# ── "If sold" preview (metron-ops#208) ──────────────────────────────────────────
#
# Tax math on a hypothetical sale the USER authors — holding, quantity, price and lot
# method are all inputs; nothing here picks a position or a lot. Read-only: builds the
# ledger, reads the cached close, computes, returns. It writes nothing.

# Placeholder flat rates used ONLY for a rate the caller did not supply. They are not
# anyone's actual rates and every response names which rates were placeholders so the
# UI can say so. (No per-user tax-rate preference exists yet.)
PLACEHOLDER_RATES = TaxRates(short_term=0.24, long_term=0.15, state=0.0)


class IfSoldError(ValueError):
    """The hypothetical can't be measured as given (no lots, no price, bad quantity…)."""


@dataclass
class IfSoldLot:
    """One open lot of the holding, as the specific-lot picker shows it."""

    lot_index: int
    open_date: date
    quantity: float
    cost_per_share: float
    cost_basis: float
    holding_days: int
    term: str


@dataclass
class IfSoldPreview:
    as_of: date
    ticker: str
    currency: str
    price: float
    price_source: str  # "latest_close" | "user"
    price_as_of: date | None
    quantity_held: float
    rates: TaxRates
    rates_placeholder: list[str]  # names of rates that fell back to PLACEHOLDER_RATES
    open_lots: list[IfSoldLot]
    sale: HypotheticalSale
    fifo: HypotheticalSale  # the FIFO baseline for the same quantity + price
    delta_vs_fifo: SaleDelta | None  # set for a specific-lot hypothetical only
    n_accounts_excluded: int = 0
    # The ticker has broker history in scope that starts mid-position — those shares
    # can't be dated into lots, so they are not part of this preview.
    history_incomplete: bool = False


def resolve_rates(
    short_term: float | None, long_term: float | None, state: float | None
) -> tuple[TaxRates, list[str]]:
    """Caller-supplied rates, with ``PLACEHOLDER_RATES`` filling any left out. Returns
    the rates and the names of those that are placeholders."""
    supplied = {"short_term": short_term, "long_term": long_term, "state": state}
    placeholder = [name for name, value in supplied.items() if value is None]
    values = {name: getattr(PLACEHOLDER_RATES, name) if value is None else value for name, value in supplied.items()}
    try:
        return TaxRates(**values), placeholder
    except ValueError as e:
        raise IfSoldError(str(e)) from None


def if_sold_preview(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    ticker: str,
    *,
    today: date,
    rates: TaxRates,
    rates_placeholder: Sequence[str] = (),
    quantity: float | None = None,
    price: float | None = None,
    method: LotMethod = LotMethod.FIFO,
    picks: Sequence[LotPick] | None = None,
    taxable_only: bool = True,
    selected_account_ids: Collection[uuid.UUID] | None = None,
) -> IfSoldPreview:
    """Measure a hypothetical sale of ``ticker`` over the (taxable) account scope.

    ``quantity`` defaults to the full position and ``price`` to the latest cached close.
    Lots are the ledger's open lots across the scoped accounts, oldest first; FIFO
    relieves them in that order (brokers relieve per account — narrowing the selection to
    one account matches a single broker's FIFO). Amounts are in the security's own
    currency, not converted.
    """
    symbol = ticker.strip()
    if not symbol:
        raise IfSoldError("Enter a ticker.")
    account_ids, n_excluded = _tax_scope(
        session, tenant_id, portfolio_id, taxable_only=taxable_only, selected_account_ids=selected_account_ids
    )
    ledger, incomplete = analytics.load_ledger(session, tenant_id, portfolio_id, account_ids=account_ids)
    key = next((t for t in ledger.open_lots if t.upper() == symbol.upper()), None)
    lots = [lot for lot in ledger.open_lots.get(key, []) if lot.quantity > 0] if key else []
    history_incomplete = any(i.ticker and i.ticker.upper() == symbol.upper() for i in incomplete)
    if not lots:
        scope = "the taxable accounts in scope" if taxable_only else "the accounts in scope"
        note = " Its imported history starts mid-position, so its lots can't be dated." if history_incomplete else ""
        raise IfSoldError(f"No open lots for {symbol.upper()} in {scope}.{note}")
    symbol = key or symbol

    currency = analytics._currency_by_symbol(session, [symbol]).get(symbol, "USD")
    price_as_of: date | None = None
    if price is None:
        point = price_service.latest_close_by_symbol(
            session, [symbol], currency_by_symbol={symbol: currency}
        ).get(symbol)
        if point is None:
            raise IfSoldError(f"No cached close for {symbol} — enter a price for the hypothetical.")
        price, price_as_of, price_source = point.close, point.bar_date, "latest_close"
    else:
        price_source = "user"
    held = sum(lot.quantity for lot in lots)
    qty = held if quantity is None else quantity

    try:
        fifo = hypothetical_sale(lots, quantity=qty, price=price, asof=today, rates=rates)
        sale = (
            fifo
            if method is LotMethod.FIFO
            else hypothetical_sale(
                lots, quantity=qty, price=price, asof=today, rates=rates, method=method, picks=picks
            )
        )
    except ValueError as e:
        raise IfSoldError(str(e)) from None

    open_rows = []
    for i, lot in enumerate(lots):
        days = (today - lot.open_date).days
        open_rows.append(
            IfSoldLot(
                lot_index=i,
                open_date=lot.open_date,
                quantity=lot.quantity,
                cost_per_share=lot.cost_per_share,
                cost_basis=lot.cost_basis,
                holding_days=days,
                term=classify_term(days),
            )
        )
    return IfSoldPreview(
        as_of=today,
        ticker=symbol,
        currency=currency,
        price=price,
        price_source=price_source,
        price_as_of=price_as_of,
        quantity_held=held,
        rates=rates,
        rates_placeholder=list(rates_placeholder),
        open_lots=open_rows,
        sale=sale,
        fifo=fifo,
        delta_vs_fifo=None if method is LotMethod.FIFO else sale_delta(sale, fifo),
        n_accounts_excluded=n_excluded,
        history_incomplete=history_incomplete,
    )
