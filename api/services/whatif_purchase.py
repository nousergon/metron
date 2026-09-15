"""What-if purchase (metron-ops-I311) — a before/after IMPACT PREVIEW of one hypothetical
purchase, never a suggestion of what to buy. The user types a ticker and an amount (or a
share count); everything here is deterministic arithmetic over the portfolio's own
holdings plus that one hypothetical line — no ranking, no score, no generated text
(intelligence-doctrine layers 1-2: nothing here is a tool-using generation call, so layers
3-5 don't apply, but the "no advice" line still holds at the copy layer in the panel).

Concentration math mirrors ``portfolio_analytics.domain.diagnostics._concentration`` (and
its client-side port, ``web/lib/whatif.ts``) so a position's HHI/top-N reads the same
number everywhere it's shown. Dividend yield on cost is the standard aggregate — total
projected annual dividend income divided by total cost basis, NOT a weighted average of
per-name yields (which double-counts scale) — from the free EDGAR-sourced fundamentals
artifact (metron-ops#22), never a live vendor fetch.

Pricing an unheld ticker: the spine price when the deployment is feed-entitled (the
``feed`` data source — a licensed EOD close, the same source ``deploy_cash`` and
``risk``/``attribution`` already read); otherwise the caller-supplied price, since a
no-feed beta build has no licensed quote for a name the user doesn't already hold
(metron-ops#52 — zero yfinance-derived data reaches a beta surface).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from api.services import analytics
from api.services import classifications as classifications_service
from api.services import fundamentals as fundamentals_service
from api.services import prices as price_service
from api.services import sectors as sectors_service

UNCLASSIFIED_SECTOR = "Unclassified"
UNASSIGNED_ACCOUNT = "New purchase (unassigned)"


@dataclass(frozen=True)
class ConcentrationSnapshot:
    n_positions: int
    hhi: float
    effective_n: float
    top5_share: float
    top10_share: float
    max_position_ticker: str | None
    max_position_weight: float


@dataclass(frozen=True)
class MixRow:
    key: str
    weight: float


@dataclass(frozen=True)
class TaxLotPreview:
    symbol: str
    shares: float
    price: float
    trade_date: date
    cost_basis: float


@dataclass(frozen=True)
class WhatIfSnapshot:
    concentration: ConcentrationSnapshot
    sector_mix: tuple[MixRow, ...]
    asset_class_mix: tuple[MixRow, ...]
    account_mix: tuple[MixRow, ...]
    dividend_yield_on_cost: float | None


@dataclass
class WhatIfPlan:
    as_of: date
    symbol: str
    shares: float
    usd: float
    price: float
    price_source: str  # "held" | "spine" | "user_entered"
    price_as_of: date | None
    before: WhatIfSnapshot
    after: WhatIfSnapshot
    beta_available: bool
    tax_lot: TaxLotPreview
    portfolio_value: float
    base_currency: str
    skipped: list[dict] = field(default_factory=list)


class WhatIfPurchaseError(ValueError):
    """A caller error (unpriceable ticker, no amount/shares, …) — 422 at the router."""


def _concentration(weights: list[tuple[str, float]]) -> ConcentrationSnapshot:
    positive = sorted((r for r in weights if r[1] > 0), key=lambda r: -r[1])
    if not positive:
        return ConcentrationSnapshot(0, 0.0, 0.0, 0.0, 0.0, None, 0.0)
    hhi = sum(w * w for _, w in positive)
    return ConcentrationSnapshot(
        n_positions=len(positive),
        hhi=hhi,
        effective_n=(1.0 / hhi) if hhi > 0 else 0.0,
        top5_share=sum(w for _, w in positive[:5]),
        top10_share=sum(w for _, w in positive[:10]),
        max_position_ticker=positive[0][0],
        max_position_weight=positive[0][1],
    )


def _mix(totals: dict[str, float], basis: float) -> tuple[MixRow, ...]:
    if basis <= 0:
        return ()
    rows = [MixRow(key=k, weight=v / basis) for k, v in totals.items() if v > 0]
    return tuple(sorted(rows, key=lambda r: -r.weight))


def _snapshot(
    mv_by_ticker: dict[str, float],
    basis: float,
    *,
    sector_of: dict[str, str | None],
    asset_class_of: dict[str, str],
    account_mv: dict[str, float],
    div_dollars_by_ticker: dict[str, float],
    cost_basis_by_ticker: dict[str, float],
) -> WhatIfSnapshot:
    conc = _concentration([(t, (mv / basis) if basis > 0 else 0.0) for t, mv in mv_by_ticker.items()])
    sector_mv: dict[str, float] = {}
    asset_mv: dict[str, float] = {}
    for t, mv in mv_by_ticker.items():
        sector_mv[sector_of.get(t) or UNCLASSIFIED_SECTOR] = sector_mv.get(sector_of.get(t) or UNCLASSIFIED_SECTOR, 0.0) + mv
        asset_mv[asset_class_of.get(t, "other")] = asset_mv.get(asset_class_of.get(t, "other"), 0.0) + mv

    total_div = sum(v for t, v in div_dollars_by_ticker.items() if t in mv_by_ticker)
    total_cost = sum(v for t, v in cost_basis_by_ticker.items() if t in mv_by_ticker and t in div_dollars_by_ticker)
    div_yield_on_cost = (total_div / total_cost) if total_cost > 0 else None

    return WhatIfSnapshot(
        concentration=conc,
        sector_mix=_mix(sector_mv, basis),
        asset_class_mix=_mix(asset_mv, basis),
        account_mix=_mix(account_mv, basis),
        dividend_yield_on_cost=div_yield_on_cost,
    )


def build_whatif(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    symbol: str,
    *,
    amount_usd: float | None = None,
    shares: float | None = None,
    user_price: float | None = None,
    account_label: str | None = None,
    feed_entitled: bool,
    as_of: date | None = None,
    price_reader=None,
    fundamentals_reader=None,
) -> WhatIfPlan:
    """Before/after impact of buying ``symbol`` with ``amount_usd`` (converted to whole
    shares at the resolved price) or an exact ``shares`` count — exactly one of the two is
    given. Never chooses the ticker; the caller always does."""
    symbol = symbol.strip().upper()
    if not symbol:
        raise WhatIfPurchaseError("Enter a ticker.")
    if (amount_usd is None) == (shares is None):
        raise WhatIfPurchaseError("Give either an amount or a share count, not both.")

    as_of = as_of or date.today()
    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    base_currency = analytics._base_currency(session, portfolio_id)
    held_by_ticker = {h.ticker: h for h in held if h.ticker}
    portfolio_value = sum(h.market_value for h in held if h.market_value is not None)

    existing = held_by_ticker.get(symbol)
    price_as_of: date | None = None
    if existing is not None and existing.last_price is not None:
        price = float(existing.last_price)
        price_source = "held"
        price_as_of = existing.last_price_date
    elif feed_entitled:
        reader = price_reader if price_reader is not None else price_service.latest_close_by_symbol
        point = reader(session, [symbol], currency_by_symbol={symbol: base_currency}).get(symbol)
        if point is None or not point.close or point.close <= 0:
            raise WhatIfPurchaseError(f"No cached price for {symbol}. Enter a price.")
        price = float(point.close)
        price_source = "spine"
        price_as_of = getattr(point, "bar_date", None)
    else:
        if user_price is None or user_price <= 0:
            raise WhatIfPurchaseError(f"{symbol} isn't held and this build has no licensed feed — enter a price.")
        price = float(user_price)
        price_source = "user_entered"

    if shares is not None:
        buy_shares = float(shares)
    else:
        buy_shares = float(amount_usd) / price if price > 0 else 0.0
    if buy_shares <= 0:
        raise WhatIfPurchaseError("The purchase must be a positive amount or share count.")
    usd = round(buy_shares * price, 2)
    basis = portfolio_value + usd

    universe = sorted(set(held_by_ticker) | {symbol})
    sector_of = sectors_service.sectors_by_symbol(session, universe)
    overrides = classifications_service.overrides_by_symbol(session, tenant_id, universe)
    for sym, ov in overrides.items():
        if ov and ov.sector:
            sector_of[sym] = ov.sector
    meta = analytics._security_meta_by_symbol(session, universe, currency_by_symbol=dict.fromkeys(universe, base_currency))
    asset_class_of = {
        sym: analytics.classify_security_type(meta.get(sym, (None, None))[0], sym, meta.get(sym, (None, None))[1])
        for sym in universe
    }

    fsnap = (fundamentals_reader() if fundamentals_reader is not None else fundamentals_service.load_fundamentals())
    fund_by_symbol = fsnap.by_symbol if hasattr(fsnap, "by_symbol") else {}

    flat = analytics.valued_holdings_by_account_flat(session, tenant_id, portfolio_id)
    account_mv_before: dict[str, float] = {}
    for h in flat:
        if h.market_value is not None:
            account_mv_before[h.account_label or "Unassigned"] = account_mv_before.get(h.account_label or "Unassigned", 0.0) + h.market_value
    dest_account = account_label or UNASSIGNED_ACCOUNT
    account_mv_after = dict(account_mv_before)
    account_mv_after[dest_account] = account_mv_after.get(dest_account, 0.0) + usd

    mv_before = {h.ticker: h.market_value for h in held if h.market_value is not None}
    mv_after = dict(mv_before)
    mv_after[symbol] = mv_after.get(symbol, 0.0) + usd

    div_dollars: dict[str, float] = {}
    cost_basis_before: dict[str, float] = {h.ticker: (h.cost_basis_base or 0.0) for h in held if h.ticker}
    cost_basis_after = dict(cost_basis_before)
    cost_basis_after[symbol] = cost_basis_after.get(symbol, 0.0) + usd
    for sym, mv in mv_after.items():
        fund = fund_by_symbol.get(sym)
        if fund is not None and fund.dividend_yield is not None:
            div_dollars[sym] = mv * fund.dividend_yield

    before = _snapshot(
        mv_before, portfolio_value, sector_of=sector_of, asset_class_of=asset_class_of,
        account_mv=account_mv_before, div_dollars_by_ticker=div_dollars, cost_basis_by_ticker=cost_basis_before,
    )
    after = _snapshot(
        mv_after, basis, sector_of=sector_of, asset_class_of=asset_class_of,
        account_mv=account_mv_after, div_dollars_by_ticker=div_dollars, cost_basis_by_ticker=cost_basis_after,
    )

    return WhatIfPlan(
        as_of=as_of, symbol=symbol, shares=buy_shares, usd=usd, price=price, price_source=price_source,
        price_as_of=price_as_of, before=before, after=after, beta_available=feed_entitled,
        tax_lot=TaxLotPreview(symbol=symbol, shares=buy_shares, price=price, trade_date=as_of, cost_basis=usd),
        portfolio_value=round(portfolio_value, 2), base_currency=base_currency,
    )
