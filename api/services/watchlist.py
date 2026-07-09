"""Watchlist — tickers the user tracks but doesn't necessarily hold (metron-ops#42).

Positions-optional, so the product is useful with zero account data. Each entry carries the
symbol + reference data (name / sector / country / next earnings, from the Security master)
and whether it's currently held, plus — on a feed-entitled build — the SAME valuation /
fundamentals / balance-sheet / technicals / consensus / attractiveness metrics the Holdings
table shows (metron-ops#121), via ``api.services.metrics_enrichment`` keyed purely by
ticker. No live price and NO position economics (quantity/cost/market value/P&L) ever
attach to a watchlist entry — comparison-only, structurally isolated from NAV/performance
(those read the Position table directly and never call this module or metrics_enrichment).
Adding a symbol caches a Security row so its reference data can resolve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics, metrics_enrichment
from api.services import classifications as classifications_service
from api.services import countries as countries_service
from api.services import prices as price_service
from api.services import sectors as sectors_service


@dataclass
class WatchlistEntry:
    symbol: str
    name: str | None
    sector: str | None
    next_earnings_date: date | None
    held: bool
    note: str | None = None
    country: str | None = None
    # Same Holdings-metrics field set as ``analytics.Holding`` (market_cap through
    # attractiveness_coverage below) — populated only on a feed-entitled build, via the SAME
    # metrics_enrichment.enrich_metrics() the Holdings endpoint uses. None off-feed or on a
    # coverage gap, never fabricated. Deliberately NO quantity/avg_cost/market_value/P&L
    # fields — a watchlist entry has no position, so there is nothing to sum into NAV.
    market_cap: float | None = None
    pe: float | None = None
    fwd_pe: float | None = None
    eps: float | None = None
    fwd_eps: float | None = None
    pb: float | None = None
    ps: float | None = None
    ev_ebitda: float | None = None
    ebitda: float | None = None
    peg: float | None = None
    div_yield: float | None = None
    rev_growth: float | None = None
    earnings_growth: float | None = None
    gross_margin: float | None = None
    op_margin: float | None = None
    roe: float | None = None
    roa: float | None = None
    beta: float | None = None
    cash: float | None = None
    debt: float | None = None
    net_debt: float | None = None
    debt_to_equity: float | None = None
    net_debt_to_ebitda: float | None = None
    current_ratio: float | None = None
    quick_ratio: float | None = None
    fcf: float | None = None
    rsi_14: float | None = None
    macd_hist: float | None = None
    pct_to_ma_50: float | None = None
    pct_to_ma_200: float | None = None
    pct_in_52w_range: float | None = None
    mom_20d: float | None = None
    consensus_rating: str | None = None
    consensus_score: float | None = None
    price_target_mean: float | None = None
    price_target_median: float | None = None
    price_target_upside: float | None = None
    num_analysts: int | None = None
    news_sentiment: float | None = None
    news_articles: int | None = None
    attractiveness: float | None = None
    attractiveness_coverage: int | None = None


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def list_watchlist(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, feed_entitled: bool = False
) -> list[WatchlistEntry]:
    """The portfolio's watchlist, enriched with reference data + a held flag, plus — on a
    feed-entitled build — the same Holdings metrics (valuation/fundamentals/balance-sheet/
    technicals/consensus/attractiveness) via metrics_enrichment, keyed purely by ticker. No
    live price — un-held tickers have no price source until the licensed Pro feed lands, so
    price_target_upside (which needs a live price) stays a coverage gap for watchlist
    entries even on a feed-entitled build."""
    items = session.scalars(
        select(models.WatchlistItem)
        .where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
        )
        .order_by(models.WatchlistItem.symbol)
    ).all()
    if not items:
        return []
    symbols = [i.symbol for i in items]
    meta = _security_meta(session, symbols)
    held_symbols = {h.ticker for h in analytics.holdings(session, tenant_id, portfolio_id)}

    # Sector/country classification — the SAME resolution the Holdings table uses (data-spine
    # reference data, tenant overrides win), so a ticker classifies identically whether it's
    # held or only watched.
    sectors_service.ensure_sectors(session, symbols)
    countries_service.ensure_countries(session, symbols)
    sector_of = sectors_service.sectors_by_symbol(session, symbols)
    country_of = countries_service.countries_by_symbol(session, symbols)
    overrides = classifications_service.overrides_by_symbol(session, tenant_id, symbols)

    # Shell holdings (zero position economics) — purely the vehicle for metrics_enrichment,
    # the SAME per-ticker valuation/fundamentals/technicals/consensus/attractiveness pipeline
    # the Holdings endpoint uses. These shells are local-only and never passed to
    # analytics.valued_holdings / performance.record_snapshot / attribution.compute_attribution
    # — the functions that compute NAV/TWR/allocation read the Position table directly, so a
    # watchlist-only ticker structurally cannot leak into any portfolio aggregate.
    shells = {
        item.symbol: analytics.Holding(ticker=item.symbol, quantity=0.0, avg_cost=0.0, cost_basis=0.0)
        for item in items
    }
    for symbol, h in shells.items():
        ov = overrides.get(symbol)
        h.sector = (ov.sector if ov and ov.sector else None) or sector_of.get(symbol)
        h.country = (ov.country if ov and ov.country else None) or country_of.get(symbol)
    if feed_entitled:
        metrics_enrichment.enrich_metrics(session, list(shells.values()))

    out: list[WatchlistEntry] = []
    for item in items:
        name, sector, earnings = meta.get(item.symbol, (None, None, None))
        h = shells[item.symbol]
        out.append(
            WatchlistEntry(
                symbol=item.symbol,
                name=name,
                sector=h.sector or sector,
                next_earnings_date=earnings,
                held=item.symbol in held_symbols,
                note=item.note,
                country=h.country,
                market_cap=h.market_cap,
                pe=h.pe,
                fwd_pe=h.fwd_pe,
                eps=h.eps,
                fwd_eps=h.fwd_eps,
                pb=h.pb,
                ps=h.ps,
                ev_ebitda=h.ev_ebitda,
                ebitda=h.ebitda,
                peg=h.peg,
                div_yield=h.div_yield,
                rev_growth=h.rev_growth,
                earnings_growth=h.earnings_growth,
                gross_margin=h.gross_margin,
                op_margin=h.op_margin,
                roe=h.roe,
                roa=h.roa,
                beta=h.beta,
                cash=h.cash,
                debt=h.debt,
                net_debt=h.net_debt,
                debt_to_equity=h.debt_to_equity,
                net_debt_to_ebitda=h.net_debt_to_ebitda,
                current_ratio=h.current_ratio,
                quick_ratio=h.quick_ratio,
                fcf=h.fcf,
                rsi_14=h.rsi_14,
                macd_hist=h.macd_hist,
                pct_to_ma_50=h.pct_to_ma_50,
                pct_to_ma_200=h.pct_to_ma_200,
                pct_in_52w_range=h.pct_in_52w_range,
                mom_20d=h.mom_20d,
                consensus_rating=h.consensus_rating,
                consensus_score=h.consensus_score,
                price_target_mean=h.price_target_mean,
                price_target_median=h.price_target_median,
                price_target_upside=h.price_target_upside,
                num_analysts=h.num_analysts,
                news_sentiment=h.news_sentiment,
                news_articles=h.news_articles,
                attractiveness=h.attractiveness,
                attractiveness_coverage=h.attractiveness_coverage,
            )
        )
    return out


def add_to_watchlist(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, symbol: str, *, note: str | None = None
) -> models.WatchlistItem:
    """Add (idempotent) a symbol to the watchlist. Caches a Security row so its reference
    data can resolve. Re-adding an existing symbol updates the note only."""
    sym = _norm(symbol)
    if not sym:
        raise ValueError("symbol is required")
    price_service.ensure_security(session, sym)
    row = session.scalars(
        select(models.WatchlistItem).where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
            models.WatchlistItem.symbol == sym,
        )
    ).first()
    if row is None:
        row = models.WatchlistItem(tenant_id=tenant_id, portfolio_id=portfolio_id, symbol=sym, note=note)
        session.add(row)
    elif note is not None:
        row.note = note
    session.commit()
    session.refresh(row)
    return row


def remove_from_watchlist(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, symbol: str
) -> bool:
    """Remove a symbol from the watchlist. Returns True if a row was deleted."""
    row = session.scalars(
        select(models.WatchlistItem).where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
            models.WatchlistItem.symbol == _norm(symbol),
        )
    ).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _security_meta(
    session: Session, symbols: list[str]
) -> dict[str, tuple[str | None, str | None, date | None]]:
    """``{symbol: (name, sector, next_earnings_date)}`` from the Security master."""
    rows = session.execute(
        select(
            models.Security.symbol,
            models.Security.name,
            models.Security.sector,
            models.Security.next_earnings_date,
        )
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, tuple[str | None, str | None, date | None]] = {}
    for symbol, name, sector, earnings in rows:
        out.setdefault(symbol, (name, sector, earnings))
    return out
