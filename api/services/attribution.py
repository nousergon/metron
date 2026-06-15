"""Brinson-Fachler sector attribution of the portfolio vs SPY (C2-6c-2).

Explains a portfolio's active return (vs SPY) over a trailing window as the sum of
three sector effects — **allocation** (over/under-weighting sectors), **selection**
(picking within a sector), and **interaction** — via
``alpha_engine_lib.quant.attribution.brinson_fachler``.

Inputs, all sourced (never fabricated):
  - **portfolio sector weights** — market-value shares of each GICS sector, over the
    holdings whose sector resolves (the unclassified remainder is surfaced as a
    coverage gap, not attributed to a guess);
  - **portfolio sector returns** — market-value-weighted window return of the holdings
    in each sector, from the cached daily closes;
  - **benchmark sector weights** — SPY's live GICS sector weights (yfinance);
  - **benchmark sector returns** — each sector's SPDR ETF (XLK/XLF/…) window return
    from the price cache.

A sector with no priced holding, or a window the cache can't span, drops out WITH a
reason rather than producing a bogus effect.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from alpha_engine_lib.quant.attribution import BrinsonResult, brinson_fachler
from sqlalchemy.orm import Session

from api.services import analytics
from api.services import prices as price_service
from api.services import sectors as sector_service
from portfolio_analytics.prices import ClosePoint, HistorySource
from portfolio_analytics.sectors import (
    SECTOR_ETF,
    BenchmarkSource,
    SectorSource,
    fetch_benchmark_sector_weights,
)

BENCHMARK = "SPY"


@dataclass
class SectorEffect:
    sector: str
    port_weight: float
    bench_weight: float
    port_return: float | None
    bench_return: float | None
    allocation: float
    selection: float
    interaction: float
    total: float


@dataclass
class AttributionSummary:
    computable: bool
    benchmark: str = BENCHMARK
    reason: str | None = None
    # Set only when not computable because the product tier / data feed excludes the
    # feature — the cheapest tier that would unlock it (drives the entitlement upsell).
    required_tier: str | None = None
    as_of: date | None = None
    start_date: date | None = None
    lookback_days: int = 0
    coverage: float = 0.0  # priced-and-classified MV / total MV
    n_sectors: int = 0
    portfolio_return: float | None = None
    benchmark_return: float | None = None
    active_return: float | None = None
    allocation: float | None = None
    selection: float | None = None
    interaction: float | None = None
    sectors: list[SectorEffect] = field(default_factory=list)


def _window_return(series: list[ClosePoint] | None, start: date) -> float | None:
    """Total return over ``[start, latest]`` from a date-ascending close series.

    Anchors on the first close on/after ``start`` (falling back to the earliest
    available close when the cache doesn't reach ``start``), against the latest close.
    None if there aren't ≥2 points or the anchor is non-positive."""
    if not series or len(series) < 2:
        return None
    after = [p for p in series if p.bar_date >= start]
    first = after[0].close if after else series[0].close
    last = series[-1].close
    if first <= 0:
        return None
    return last / first - 1.0


def _portfolio_sector_aggregates(
    holdings: list[analytics.Holding],
    sector_of: dict[str, str | None],
    returns_of: dict[str, float],
) -> tuple[dict[str, float], dict[str, float], float]:
    """Portfolio sector weights + market-value-weighted sector returns + coverage.

    Only holdings whose sector maps to a known GICS sector are attributable; weights
    are over that covered market value (so they sum to 1), and ``coverage`` is covered
    MV / total priced MV (an unmapped or sector-less holding is surfaced, not dropped).
    A sector's return weights its holdings by their MV share *among holdings that have
    a computed return* — a sector with no priced-and-returnable holding is omitted from
    the returns map (its weight still counts toward allocation)."""
    total_mv = sum(h.market_value for h in holdings)
    by_sector_mv: dict[str, float] = {}
    for h in holdings:
        sec = sector_of.get(h.ticker)
        if sec in SECTOR_ETF:
            by_sector_mv[sec] = by_sector_mv.get(sec, 0.0) + h.market_value
    covered_mv = sum(by_sector_mv.values())
    if covered_mv <= 0:
        return {}, {}, 0.0
    weights = {s: mv / covered_mv for s, mv in by_sector_mv.items()}

    returns: dict[str, float] = {}
    for sec in by_sector_mv:
        priced = [h for h in holdings if sector_of.get(h.ticker) == sec and h.ticker in returns_of]
        mv = sum(h.market_value for h in priced)
        if mv <= 0:
            continue
        returns[sec] = sum(h.market_value / mv * returns_of[h.ticker] for h in priced)

    coverage = covered_mv / total_mv if total_mv > 0 else 0.0
    return weights, returns, coverage


def _normalize_benchmark_weights(raw: dict[str, float]) -> dict[str, float]:
    """Restrict raw benchmark weights to the known GICS sectors and renormalize to 1."""
    kept = {s: w for s, w in raw.items() if s in SECTOR_ETF and w > 0}
    total = sum(kept.values())
    return {s: w / total for s, w in kept.items()} if total > 0 else {}


def compute_attribution(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    lookback_days: int = 90,
    do_backfill: bool = False,
    price_source: HistorySource | None = None,
    sector_source: SectorSource | None = None,
    benchmark_source: BenchmarkSource | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> AttributionSummary:
    """Brinson-Fachler sector attribution of the market-value-weighted portfolio vs
    SPY over the trailing ``lookback_days``. ``do_backfill`` (the POST path) first
    resolves holding sectors and backfills the held + SPDR-ETF history over the window;
    the GET path computes from whatever's already cached. ``account_ids`` scopes the
    holdings (portfolio sector weights/returns) to the selected accounts; None = whole
    portfolio (the SPY benchmark + SPDR history stay global)."""
    start = today - timedelta(days=lookback_days)
    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    priced = [h for h in held if h.market_value and h.market_value > 0]
    if not priced:
        return AttributionSummary(False, reason="No priced holdings — refresh prices first.", lookback_days=lookback_days)
    tickers = [h.ticker for h in priced]
    etfs = list(SECTOR_ETF.values())

    if do_backfill:
        sector_service.ensure_sectors(session, tickers, source=sector_source)
        for etf in etfs:
            price_service.ensure_security(session, etf)
        price_service.backfill_prices(session, [*tickers, *etfs], start, today, source=price_source)

    raw_bench = fetch_benchmark_sector_weights(source=benchmark_source)
    w_b = _normalize_benchmark_weights(raw_bench)
    if not w_b:
        return AttributionSummary(
            False,
            reason="Benchmark sector weights unavailable — compute attribution to fetch them.",
            lookback_days=lookback_days,
        )

    sector_of = sector_service.sectors_by_symbol(session, tickers)
    hist = price_service.close_history_by_symbol(session, [*tickers, *etfs])
    holding_returns = {t: r for t in tickers if (r := _window_return(hist.get(t), start)) is not None}

    w_p, r_p, coverage = _portfolio_sector_aggregates(priced, sector_of, holding_returns)
    if not w_p:
        return AttributionSummary(
            False,
            reason="No holdings map to a GICS sector yet — compute attribution to classify them.",
            lookback_days=lookback_days,
        )
    r_b = {sec: r for sec, etf in SECTOR_ETF.items() if (r := _window_return(hist.get(etf), start)) is not None}
    if not r_p or not r_b:
        return AttributionSummary(
            False,
            reason="Not enough price history yet — compute attribution to backfill it.",
            lookback_days=lookback_days,
        )

    result: BrinsonResult = brinson_fachler(w_p, r_p, w_b, r_b)
    effects = [
        SectorEffect(
            sector=g.group,
            port_weight=w_p.get(g.group, 0.0),
            bench_weight=w_b.get(g.group, 0.0),
            port_return=r_p.get(g.group),
            bench_return=r_b.get(g.group),
            allocation=g.allocation,
            selection=g.selection,
            interaction=g.interaction,
            total=g.allocation + g.selection + g.interaction,
        )
        for g in result.groups
    ]
    effects.sort(key=lambda e: e.total)  # biggest detractors first → read at a glance
    return AttributionSummary(
        computable=True,
        as_of=today,
        start_date=start,
        lookback_days=lookback_days,
        coverage=coverage,
        n_sectors=len(effects),
        portfolio_return=result.portfolio_return,
        benchmark_return=result.benchmark_return,
        active_return=result.portfolio_return - result.benchmark_return,
        allocation=result.allocation,
        selection=result.selection,
        interaction=result.interaction,
        sectors=effects,
    )
