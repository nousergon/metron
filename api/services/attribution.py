"""Brinson-Fachler sector attribution of the portfolio vs SPY (C2-6c-2).

Explains a portfolio's active return (vs SPY) over a trailing window as the sum of
three sector effects — **allocation** (over/under-weighting sectors), **selection**
(picking within a sector), and **interaction** — via
``nousergon_lib.quant.attribution.brinson_fachler``.

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

**One window for every leg** (metron-ops-I372). Every return here — each holding, each
sector ETF — is measured over the SAME dated interval ``[start_date, as_of]``: the anchor
is the first session every sector-ETF series reaches on/after the requested start, the end
is the last session EVERY one of them reaches. A holding whose cached series does not span
that interval (short history, or stale) is not measured over its own shorter interval —
it is left out of the attributable population and named in ``unattributed`` with the
reason. ``coverage`` is the share of portfolio market value that was actually attributed
(classified AND returnable), and the sector weights are over that same population, so
weights and returns describe one set of holdings. ``as_of`` is the common end — the
freshness of the stalest required leg, never the freshest.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from nousergon_lib.quant.attribution import BrinsonResult, brinson_fachler
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

# metron-ops-I346: sector-level Brinson-Fachler now runs against either proxy — SPY (the
# S&P 500) or QQQ (the Nasdaq-100), matching the two indexes the name-level benchmark-gap
# feature covers (api/services/benchmark_gap.py) — the two levels compose: sector
# allocation/selection here, name-level active contribution there.
BENCHMARKS: dict[str, str] = {"SPY": "S&P 500", "QQQ": "Nasdaq 100"}
BENCHMARK = "SPY"  # default, kept for backward compatibility


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
class UnattributedHolding:
    """A priced holding whose market value is NOT in the attribution, and why."""

    ticker: str
    market_value: float
    # "no_sector" | "no_history" | "short_history" (no close on/before the window start) |
    # "stale" (no close at the window end) | "invalid_price"
    reason: str


@dataclass
class AttributionSummary:
    computable: bool
    benchmark: str = BENCHMARK
    reason: str | None = None
    # Set only when not computable because the product tier / data feed excludes the
    # feature — the cheapest tier that would unlock it (drives the entitlement upsell).
    required_tier: str | None = None
    # The common window every return was measured over (metron-ops-I372): ``start_date`` is
    # the actual anchor session and ``as_of`` the common end session. ``requested_start``
    # is ``today - lookback_days``, kept so a short cache is visible rather than implied.
    as_of: date | None = None
    start_date: date | None = None
    requested_start: date | None = None
    lookback_days: int = 0
    coverage: float = 0.0  # attributed (classified AND returnable over the window) MV / total priced MV
    n_sectors: int = 0
    portfolio_return: float | None = None
    benchmark_return: float | None = None
    active_return: float | None = None
    allocation: float | None = None
    selection: float | None = None
    interaction: float | None = None
    sectors: list[SectorEffect] = field(default_factory=list)
    unattributed: list[UnattributedHolding] = field(default_factory=list)


def _close_on_or_before(series: list[ClosePoint], when: date) -> ClosePoint | None:
    found = None
    for p in series:
        if p.bar_date > when:
            break
        found = p
    return found


def _common_window(reference: list[list[ClosePoint]], start: date) -> tuple[date, date] | None:
    """``(anchor, end)`` every reference series spans, or None when they share no interval.

    ``anchor`` = the latest of the series' first sessions on/after ``start`` (so every
    reference series has a close on it or before it within the read); ``end`` = the
    earliest of their last sessions (so every one has a close there). Freshness is the
    stalest required leg's, by construction."""
    firsts = []
    for series in reference:
        after = [p.bar_date for p in series if p.bar_date >= start]
        if not after:
            return None
        firsts.append(after[0])
    if not firsts:
        return None
    anchor = max(firsts)
    end = min(series[-1].bar_date for series in reference)
    return (anchor, end) if end > anchor else None


def _window_return(series: list[ClosePoint] | None, anchor: date, end: date) -> tuple[float | None, str | None]:
    """Return over exactly ``[anchor, end]``, or ``(None, reason)``.

    Both ends are the close on/before that date, and the series must actually reach both:
    a first bar after ``anchor`` is ``short_history`` and a last bar before ``end`` is
    ``stale`` — measuring such a series over the part it does cover would put a different
    interval into the same sum as every other leg."""
    if not series:
        return None, "no_history"
    if series[0].bar_date > anchor:
        return None, "short_history"
    if series[-1].bar_date < end:
        return None, "stale"
    first = _close_on_or_before(series, anchor)
    last = _close_on_or_before(series, end)
    if first is None or last is None or first.close <= 0 or last.close <= 0:
        return None, "invalid_price"
    return last.close / first.close - 1.0, None


def _portfolio_sector_aggregates(
    holdings: list[analytics.Holding],
    sector_of: dict[str, str | None],
    returns_of: dict[str, float],
) -> tuple[dict[str, float], dict[str, float], float]:
    """Portfolio sector weights + market-value-weighted sector returns + coverage.

    One population (metron-ops-I372): a holding is ATTRIBUTABLE when its sector maps to a
    known GICS sector AND it has a window return. Weights are over attributable market
    value (they sum to 1), each sector's return is the MV-weighted return of its
    attributable holdings, and ``coverage`` is attributable MV / total priced MV. Before,
    weights counted classified holdings with NO return while the returns renormalized
    over the ones with a return, and coverage counted them as attributed."""
    total_mv = sum(h.market_value for h in holdings)
    attributable = [
        h for h in holdings if sector_of.get(h.ticker) in SECTOR_ETF and h.ticker in returns_of
    ]
    by_sector_mv: dict[str, float] = {}
    for h in attributable:
        sec = sector_of[h.ticker]
        by_sector_mv[sec] = by_sector_mv.get(sec, 0.0) + h.market_value
    covered_mv = sum(by_sector_mv.values())
    if covered_mv <= 0:
        return {}, {}, 0.0
    weights = {s: mv / covered_mv for s, mv in by_sector_mv.items()}
    returns = {
        sec: sum(h.market_value / mv * returns_of[h.ticker] for h in attributable if sector_of[h.ticker] == sec)
        for sec, mv in by_sector_mv.items()
    }
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
    benchmark: str = BENCHMARK,
) -> AttributionSummary:
    """Brinson-Fachler sector attribution of the market-value-weighted portfolio vs
    ``benchmark`` (SPY or QQQ — see ``BENCHMARKS``) over the trailing ``lookback_days``.
    ``do_backfill`` (the POST path) first resolves holding sectors and backfills the held
    + SPDR-ETF history over the window; the GET path computes from whatever's already
    cached. ``account_ids`` scopes the holdings (portfolio sector weights/returns) to the
    selected accounts; None = whole portfolio (the benchmark + SPDR history stay global).
    """
    if benchmark not in BENCHMARKS:
        raise ValueError(f"unknown benchmark {benchmark!r}; known: {sorted(BENCHMARKS)}")
    start = today - timedelta(days=lookback_days)
    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    priced = [h for h in held if h.market_value and h.market_value > 0]
    if not priced:
        return AttributionSummary(
            False, benchmark=benchmark, reason="No priced holdings — refresh prices first.", lookback_days=lookback_days
        )
    tickers = [h.ticker for h in priced]
    etfs = list(SECTOR_ETF.values())

    if do_backfill:
        sector_service.ensure_sectors(session, tickers, source=sector_source)
        for etf in etfs:
            price_service.ensure_security(session, etf)
        price_service.backfill_prices(session, [*tickers, *etfs], start, today, source=price_source)

    raw_bench = fetch_benchmark_sector_weights(benchmark, source=benchmark_source)
    w_b = _normalize_benchmark_weights(raw_bench)
    if not w_b:
        return AttributionSummary(
            False,
            benchmark=benchmark,
            reason="Benchmark sector weights unavailable — compute attribution to fetch them.",
            lookback_days=lookback_days,
        )

    sector_of = sector_service.sectors_by_symbol(session, tickers)
    # Bounded at ``start`` (metron-ops-I343): every return is measured from the common
    # anchor on/after ``start``, so earlier bars never change a result. Unbounded, this
    # read every cached bar of every held ticker and sector ETF.
    hist = price_service.close_history_by_symbol(session, [*tickers, *etfs], start_date=start)
    # The common window (metron-ops-I372), set by the sector-ETF legs that carry benchmark
    # weight — the benchmark side cannot be measured without them, so they bound it.
    required_etfs = [SECTOR_ETF[sec] for sec in w_b if hist.get(SECTOR_ETF[sec])]
    window = _common_window([hist[etf] for etf in required_etfs], start)
    if window is None:
        return AttributionSummary(
            False,
            benchmark=benchmark,
            reason="Not enough price history yet — compute attribution to backfill it.",
            requested_start=start,
            lookback_days=lookback_days,
        )
    anchor, end = window

    holding_returns: dict[str, float] = {}
    unattributed: list[UnattributedHolding] = []
    for h in priced:
        if sector_of.get(h.ticker) not in SECTOR_ETF:
            unattributed.append(UnattributedHolding(h.ticker, h.market_value, "no_sector"))
            continue
        ret, why = _window_return(hist.get(h.ticker), anchor, end)
        if ret is None:
            unattributed.append(UnattributedHolding(h.ticker, h.market_value, why or "no_history"))
        else:
            holding_returns[h.ticker] = ret

    w_p, r_p, coverage = _portfolio_sector_aggregates(priced, sector_of, holding_returns)
    if not w_p:
        classified = any(sector_of.get(h.ticker) in SECTOR_ETF for h in priced)
        return AttributionSummary(
            False,
            benchmark=benchmark,
            reason=(
                "No classified holding has a price history spanning the window yet — compute "
                "attribution to backfill it."
                if classified
                else "No holdings map to a GICS sector yet — compute attribution to classify them."
            ),
            requested_start=start,
            lookback_days=lookback_days,
            unattributed=unattributed,
        )
    r_b = {
        sec: r for sec, etf in SECTOR_ETF.items() if (r := _window_return(hist.get(etf), anchor, end)[0]) is not None
    }
    if not r_p or not r_b:
        return AttributionSummary(
            False,
            benchmark=benchmark,
            reason="Not enough price history yet — compute attribution to backfill it.",
            requested_start=start,
            lookback_days=lookback_days,
            unattributed=unattributed,
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
        benchmark=benchmark,
        as_of=end,
        start_date=anchor,
        requested_start=start,
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
        unattributed=sorted(unattributed, key=lambda u: -u.market_value),
    )
