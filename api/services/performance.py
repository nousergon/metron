"""Portfolio performance over the forward-recorded NAV snapshot series.

Market value can't be reconstructed for the past from cost basis alone, so — like
robodashboard — NAV history accumulates as the user refreshes prices: each refresh
records one snapshot (idempotent per day). ``performance()`` then derives time-weighted
return, cash-flow-adjusted cumulative return, and annualization from that series using
the shared ``alpha_engine_lib.quant.returns`` primitives.

Metrics are None until ≥2 snapshots exist — the caller shows "history is building",
never a fabricated number.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from alpha_engine_lib.quant.returns import ValuationPoint, annualize, time_weighted_return
from alpha_engine_lib.quant.riskstats import max_drawdown, sharpe_ratio, sortino_ratio, volatility
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from api.services import fx as fx_service
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.prices import ClosePoint, HistorySource, fetch_latest_closes

logger = logging.getLogger(__name__)

# Don't extrapolate a sub-month observation window to a yearly rate. Annualizing a few
# days of return — annualized_twr = (1+twr)^(365.25/days) − 1 — explodes for small
# `days` (e.g. +1% over 3 days → +236% annualized), and annualizing volatility/Sharpe
# from a handful of same-week returns is just as misleading. Below this span the
# ANNUALIZED figures (annualized_twr, volatility, sharpe, sortino) stay None
# ("history is building"); the window-agnostic figures (cumulative, TWR, max drawdown)
# are always shown. (metron-ops#44 — these short-window annualized values read as wrong.)
_MIN_ANNUALIZE_DAYS = 30

# Rolling risk series (metron-ops#67) — mirror the Crucible risk basket over time so the
# user can see how Sharpe/Sortino/vol/drawdown evolve. Each point uses a trailing window of
# the flow-neutralized returns: expanding until it reaches _ROLLING_WINDOW, then rolling.
# A point is emitted once the window has _ROLLING_MIN_OBS returns AND spans
# _MIN_ANNUALIZE_DAYS (the same annualization floor the headline metrics use).
_ROLLING_WINDOW = 63       # ~3 months of trading days
_ROLLING_MIN_OBS = 20

# Overview period-tile benchmarks (metron-ops#83) — the SAME ETF proxies the intraday
# strip uses (api.services.indices.INDEX_ORDER). An index VALUE carries a separate license;
# the tradeable ETF close is an ordinary equity price, so the ETF is the proxy. The
# benchmark comparison is FEED-GATED (Pro): the no-feed beta renders the tiles
# portfolio-only (the caller passes with_benchmarks=False). (symbol, display label).
BENCHMARKS: list[tuple[str, str]] = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000")]

# The Overview hero windows (metron-ops#83): (period_key, display label).
PERIOD_TILES: list[tuple[str, str]] = [("today", "Today"), ("ytd", "YTD"), ("ltm", "LTM")]

# Benchmark history is read from the price_bars cache; before computing a window we ensure
# the cache spans it (feed path only — gated by the caller). The latest cached bar may lag
# "today" by a weekend/holiday, so coverage counts as fresh within this slack.
_BENCH_COVERAGE_SLACK_DAYS = 4


def _purchase_flow(rows: list[tuple[str, float]]) -> float:
    """Net purchases for a set of (txn_type, amount) rows: a BUY brings capital INTO the
    holdings (+amount), a SELL takes it OUT (−amount)."""
    flow = 0.0
    for txn_type, amount in rows:
        if txn_type == TxnType.BUY.value:
            flow += float(amount)
        elif txn_type == TxnType.SELL.value:
            flow -= float(amount)
    return flow


def _net_purchases(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    after: date | None,
    through: date,
) -> float:
    """Net external capital into the portfolio over ``(after, through]`` — NET PURCHASES
    (ΣBUY − ΣSELL).

    Metron's NAV is the market value of HOLDINGS (no cash bucket). With no cash, a BUY is
    the moment capital enters the valued portfolio and a SELL the moment it leaves; a cash
    DEPOSIT/WITHDRAWAL doesn't move the holdings NAV (it only does once spent on a buy), and
    a reinvested dividend is a buy. So the flow TWR must neutralize is net purchases — NOT
    cash deposits. Counting only deposits (the old behavior) made a portfolio funded by
    buys read its entire contribution-driven build-up as investment return (metron-ops#44)."""
    conds = [
        models.Transaction.tenant_id == tenant_id,
        models.Account.portfolio_id == portfolio_id,
        models.Transaction.trade_date <= through,
        models.Transaction.txn_type.in_([TxnType.BUY.value, TxnType.SELL.value]),
    ]
    if after is not None:
        conds.append(models.Transaction.trade_date > after)
    rows = session.execute(
        select(models.Transaction.txn_type, models.Transaction.amount)
        .join(models.Account, models.Transaction.account_id == models.Account.id)
        .where(*conds)
    ).all()
    return _purchase_flow(rows)


def _account_net_purchases(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID, *, after: date | None, through: date
) -> float:
    """Per-ACCOUNT net purchases over ``(after, through]`` — the account-grain analogue."""
    conds = [
        models.Transaction.tenant_id == tenant_id,
        models.Transaction.account_id == account_id,
        models.Transaction.trade_date <= through,
        models.Transaction.txn_type.in_([TxnType.BUY.value, TxnType.SELL.value]),
    ]
    if after is not None:
        conds.append(models.Transaction.trade_date > after)
    rows = session.execute(
        select(models.Transaction.txn_type, models.Transaction.amount).where(*conds)
    ).all()
    return _purchase_flow(rows)


def _last_snapshot_date(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, before: date
) -> date | None:
    """The most recent portfolio NAV-snapshot date strictly before ``before`` (so a new
    snapshot's flow can span every purchase since the last one, robust to refresh gaps)."""
    return session.scalars(
        select(models.NavSnapshot.snap_date)
        .where(
            models.NavSnapshot.tenant_id == tenant_id,
            models.NavSnapshot.portfolio_id == portfolio_id,
            models.NavSnapshot.snap_date < before,
        )
        .order_by(models.NavSnapshot.snap_date.desc())
    ).first()


def _last_account_snapshot_date(
    session: Session, tenant_id: uuid.UUID, account_id: uuid.UUID, before: date
) -> date | None:
    return session.scalars(
        select(models.AccountNavSnapshot.snap_date)
        .where(
            models.AccountNavSnapshot.tenant_id == tenant_id,
            models.AccountNavSnapshot.account_id == account_id,
            models.AccountNavSnapshot.snap_date < before,
        )
        .order_by(models.AccountNavSnapshot.snap_date.desc())
    ).first()


# A daily NAV can't plausibly jump beyond this FACTOR vs the recent baseline once net
# external flows (deposits/withdrawals) are accounted for — a >3x or <1/3x move with no
# matching flow is a data error. metron-ops#74: a sync racing the daily refresh
# intermittently wrote ~12x-inflated NAVs (e.g. $4.5M vs a true ~$0.8M), whipsawing the
# Performance page's latest-NAV + max-drawdown (to −96%). Such a snapshot is flagged and
# NOT persisted (the last good value stands). Generous enough to allow a genuine
# correction (a previously-undercounted run recovering) and large market days.
_NAV_JUMP_FACTOR = 3.0
_BASELINE_SNAPSHOTS = 7  # robust baseline = median of up to this many recent snapshots


def _recent_navs(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, before: date,
    limit: int = _BASELINE_SNAPSHOTS,
) -> list[float]:
    """Up to ``limit`` NAVs strictly before ``before`` (most recent first) — the robust
    baseline for the jump guard. The median of these resists a single transient spike."""
    return [
        float(n) for n in session.scalars(
            select(models.NavSnapshot.nav)
            .where(
                models.NavSnapshot.tenant_id == tenant_id,
                models.NavSnapshot.portfolio_id == portfolio_id,
                models.NavSnapshot.snap_date < before,
            )
            .order_by(models.NavSnapshot.snap_date.desc())
            .limit(limit)
        ).all()
    ]


def _implausible_nav(nav: float, baseline_navs: list[float], flow: float) -> bool:
    """True when ``nav`` is a data error vs the recent baseline net of external flow — a
    >``_NAV_JUMP_FACTOR``x or <1/factor move the day's deposits/withdrawals can't explain.
    No baseline (the first snapshots) → never implausible."""
    if not baseline_navs:
        return False
    baseline = statistics.median(baseline_navs)
    expected = baseline + flow
    if baseline <= 0 or expected <= 0:
        return False
    ratio = nav / expected
    return ratio > _NAV_JUMP_FACTOR or ratio < 1.0 / _NAV_JUMP_FACTOR


def record_snapshot(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date, source=None
) -> models.NavSnapshot | None:
    """Record today's NAV snapshot (idempotent per day). Returns the row, or None when
    NAV isn't computable yet (no holding has a cached price → nothing to value) OR when the
    computed NAV is an implausible jump vs the recent baseline (a sync racing the refresh —
    metron-ops#74); the suspect value is logged and skipped, never persisted."""
    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    priced = [h for h in held if h.market_value is not None]
    if not priced:
        return None  # can't snapshot a NAV we can't value — never fabricate one
    nav = sum(h.market_value for h in priced)
    # Base-currency cost basis (matches the base-currency NAV); a holding with no cached
    # FX rate is excluded rather than summed at native face value.
    cost_basis = sum(h.cost_basis_base for h in held if h.cost_basis_base is not None)
    flow = _net_purchases(
        session, tenant_id, portfolio_id,
        after=_last_snapshot_date(session, tenant_id, portfolio_id, today), through=today,
    )
    baseline = _recent_navs(session, tenant_id, portfolio_id, today)
    if _implausible_nav(nav, baseline, flow):
        logger.warning(
            "metron-ops#74: skipping implausible NAV snapshot for %s — nav=%.0f, baseline median=%.0f, "
            "flow=%.0f (likely a data sync racing the refresh); keeping the last good value",
            today, nav, statistics.median(baseline), flow,
        )
        return None
    spy_point = fetch_latest_closes(["SPY"], source=source).get("SPY")
    row = _upsert_snapshot(
        session, tenant_id, portfolio_id, today,
        nav=nav, cost_basis=cost_basis, flow=flow, spy_close=spy_point.close if spy_point else None,
    )
    session.commit()
    session.refresh(row)
    return row


def _upsert_snapshot(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    when: date,
    *,
    nav: float,
    cost_basis: float,
    flow: float,
    spy_close: float | None,
) -> models.NavSnapshot:
    """Find-or-create the (portfolio, day) snapshot and set its fields. Does NOT commit
    — the caller batches the commit (one per refresh, one per reconstruction run)."""
    row = session.scalars(
        select(models.NavSnapshot).where(
            models.NavSnapshot.tenant_id == tenant_id,
            models.NavSnapshot.portfolio_id == portfolio_id,
            models.NavSnapshot.snap_date == when,
        )
    ).first()
    if row is None:
        row = models.NavSnapshot(tenant_id=tenant_id, portfolio_id=portfolio_id, snap_date=when)
        session.add(row)
    row.nav = nav
    row.cost_basis = cost_basis
    row.external_flow = flow
    if spy_close is not None:
        row.spy_close = spy_close
    return row


def repair_nav_snapshots(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID,
    *, factor: float = _NAV_JUMP_FACTOR, window: int = 5, dry_run: bool = False,
) -> dict:
    """One-off repair (metron-ops#74) for NAV snapshots a sync-vs-refresh race already
    persisted before the ``record_snapshot`` guard existed: delete rows whose NAV is a
    >``factor``x / <1/factor x outlier vs the MEDIAN of their local neighbourhood (the
    ``window`` rows either side). Idempotent — a clean series removes nothing. Returns the
    removed ``(snap_date, nav, neighbourhood_median)`` rows; ``dry_run`` reports without
    deleting. Cannot reconstruct a correct historical NAV (past market value isn't
    derivable), so it removes the corrupt points rather than rewriting them — the series
    self-heals forward as clean snapshots accrue."""
    rows = session.scalars(
        select(models.NavSnapshot)
        .where(models.NavSnapshot.tenant_id == tenant_id, models.NavSnapshot.portfolio_id == portfolio_id)
        .order_by(models.NavSnapshot.snap_date)
    ).all()
    navs = [float(r.nav) for r in rows]
    removed: list[tuple[str, float, float]] = []
    for i, r in enumerate(rows):
        others = [
            navs[j] for j in range(max(0, i - window), min(len(navs), i + window + 1))
            if j != i and navs[j] > 0
        ]
        if len(others) < 2:
            continue
        med = statistics.median(others)
        if med <= 0:
            continue
        ratio = navs[i] / med
        if ratio > factor or ratio < 1.0 / factor:
            removed.append((r.snap_date.isoformat(), round(navs[i]), round(med)))
            if not dry_run:
                session.delete(r)
    if removed and not dry_run:
        session.commit()
        logger.warning("metron-ops#74: repaired %d corrupt NAV snapshot(s): %s", len(removed), removed)
    return {"count": len(removed), "removed": removed}


def record_account_snapshots(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date, source=None
) -> int:
    """Record today's per-ACCOUNT NAV snapshots (idempotent per day). Returns the count
    written.

    The additive sibling of ``record_snapshot`` — it starts the per-account NAV history
    that can NOT be reconstructed for snapshot-sourced accounts (IBKR Flex / SnapTrade
    report current positions, not a per-account activity feed). Summing the selected
    accounts' rows on a day later yields that subset's NAV, so account-level performance
    materializes as this accrues. An account with no priced holding is skipped (never a
    fabricated NAV). Values every account in one pass via ``valued_holdings_by_account``
    (one price + FX lookup); SPY close is fetched once and shared across accounts."""
    by_account = analytics.valued_holdings_by_account(session, tenant_id, portfolio_id)
    spy_point = fetch_latest_closes(["SPY"], source=source).get("SPY")
    spy_close = spy_point.close if spy_point else None
    written = 0
    for account_id, held in by_account.items():
        priced = [h for h in held if h.market_value is not None]
        if not priced:
            continue  # can't snapshot a NAV we can't value — never fabricate one
        nav = sum(h.market_value for h in priced)
        cost_basis = sum(h.cost_basis_base for h in held if h.cost_basis_base is not None)
        flow = _account_net_purchases(
            session, tenant_id, account_id,
            after=_last_account_snapshot_date(session, tenant_id, account_id, today), through=today,
        )
        _upsert_account_snapshot(
            session, tenant_id, portfolio_id, account_id, today,
            nav=nav, cost_basis=cost_basis, flow=flow, spy_close=spy_close,
        )
        written += 1
    if written:
        session.commit()
    return written


def _upsert_account_snapshot(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_id: uuid.UUID,
    when: date,
    *,
    nav: float,
    cost_basis: float,
    flow: float,
    spy_close: float | None,
) -> models.AccountNavSnapshot:
    """Find-or-create the (account, day) snapshot and set its fields. Does NOT commit."""
    row = session.scalars(
        select(models.AccountNavSnapshot).where(
            models.AccountNavSnapshot.tenant_id == tenant_id,
            models.AccountNavSnapshot.account_id == account_id,
            models.AccountNavSnapshot.snap_date == when,
        )
    ).first()
    if row is None:
        row = models.AccountNavSnapshot(
            tenant_id=tenant_id, portfolio_id=portfolio_id, account_id=account_id, snap_date=when
        )
        session.add(row)
    row.nav = nav
    row.cost_basis = cost_basis
    row.external_flow = flow
    if spy_close is not None:
        row.spy_close = spy_close
    return row


@dataclass
class PerfPoint:
    snap_date: date
    nav: float
    external_flow: float
    spy_close: float | None


@dataclass
class RollingRiskPoint:
    """One trailing-window risk reading (metron-ops#67) — the Crucible basket over time."""

    snap_date: date
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None


@dataclass
class PerformanceSummary:
    n_snapshots: int
    first_date: date | None = None
    last_date: date | None = None
    days: int = 0
    latest_nav: float | None = None
    latest_cost_basis: float | None = None
    net_contributions: float = 0.0
    cumulative_return: float | None = None
    twr: float | None = None
    annualized_twr: float | None = None
    # Risk metrics over the flow-neutralized return series (None until ≥3 snapshots) +
    # the benchmark comparison (None until ≥2 snapshots carry an SPY close).
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    spy_return: float | None = None
    alpha: float | None = None
    # Trailing-window risk basket over time (metron-ops#67); empty until enough history.
    rolling: list[RollingRiskPoint] = field(default_factory=list)
    points: list[PerfPoint] = field(default_factory=list)
    # NAV history is an ESTIMATE when lot coverage is incomplete (metron-ops#74) — the
    # page shows a banner. estimated_note names the affected holdings.
    estimated: bool = False
    estimated_note: str | None = None


@dataclass
class BenchmarkReturn:
    """One benchmark's comparison over a tile window (metron-ops#83)."""

    symbol: str
    label: str
    ret: float | None      # benchmark % return over the window (None if uncached)
    alpha: float | None    # portfolio TWR − benchmark return (None if either is missing)


@dataclass
class PeriodTile:
    """One Overview hero tile (metron-ops#83): the aggregate holdings' performance over a
    window, plus the per-benchmark comparison. ``gain`` is the $ INVESTMENT gain (net of
    external flows — never reads a contribution as a return); ``twr`` the % time-weighted
    return over the same window. Metrics are None when the window can't be formed (history
    doesn't span it yet)."""

    period: str
    label: str
    start_date: date | None
    end_date: date | None
    gain: float | None
    twr: float | None
    benchmarks: list[BenchmarkReturn] = field(default_factory=list)


@dataclass
class PeriodTilesResult:
    tiles: list[PeriodTile] = field(default_factory=list)
    benchmarks_available: bool = False  # any benchmark history was readable (feed path)
    last_date: date | None = None


def _flow_neutralized_returns(points: list[PerfPoint]) -> list[float]:
    """Per-period flow-neutralized returns — the same neutralization the TWR uses:
    a point's NAV is recorded end-of-day (post-flow), so ``(navₜ − flowₜ) / navₜ₋₁ − 1``
    strips the day's external deposit/withdrawal, leaving pure investment return.
    Skips a period whose prior NAV is non-positive."""
    out: list[float] = []
    for i in range(1, len(points)):
        prev_nav = points[i - 1].nav
        if prev_nav > 0:
            out.append((points[i].nav - points[i].external_flow) / prev_nav - 1.0)
    return out


def _apply_risk_and_alpha(summary: PerformanceSummary, points: list[PerfPoint]) -> None:
    """Fill risk metrics (flow-neutralized) + the SPY comparison on ``summary``.

    Metron's transaction feed lets us neutralize flows properly (unlike a NAV-only
    feed): risk metrics run on the flow-neutralized return series, and max drawdown on
    its growth index — so a deposit reads as capital in, never as a return. Annualization
    uses an **empirical** periods-per-year (return count over elapsed years), robust to
    the irregular snapshot cadence rather than assuming 252 trading days. Sharpe/Sortino
    assume a 0% risk-free rate. Annualized risk stats are suppressed for a sub-month
    window (see ``_MIN_ANNUALIZE_DAYS``); max drawdown is window-agnostic and always set."""
    rets = _flow_neutralized_returns(points)
    if rets:
        # Growth index of the flow-neutralized returns → honest peak-to-trough drawdown.
        # Window-agnostic (a real drawdown at any span), so it's computed unconditionally.
        index = [1.0]
        for r in rets:
            index.append(index[-1] * (1.0 + r))
        summary.max_drawdown = max_drawdown(index)
        # Annualized vol/Sharpe/Sortino only once the window is long enough to annualize;
        # below that the empirical periods-per-year blows them up (metron-ops#44).
        if summary.days >= _MIN_ANNUALIZE_DAYS:
            years = summary.days / 365.25
            ppy = len(rets) / years
            summary.volatility = volatility(rets, periods_per_year=ppy)
            summary.sharpe = sharpe_ratio(rets, periods_per_year=ppy)
            summary.sortino = sortino_ratio(rets, periods_per_year=ppy)

    spy = [p.spy_close for p in points if p.spy_close is not None]
    if len(spy) >= 2 and spy[0] > 0:
        summary.spy_return = spy[-1] / spy[0] - 1.0
        port_return = summary.twr if summary.twr is not None else summary.cumulative_return
        if port_return is not None:
            summary.alpha = port_return - summary.spy_return


def _rolling_risk(points: list[PerfPoint]) -> list[RollingRiskPoint]:
    """Trailing-window risk basket over time (metron-ops#67). ``rets[k]`` is the return
    for the period ending at ``points[k+1]``; for each endpoint we annualize over the
    window using the same empirical periods-per-year as the headline metrics."""
    rets = _flow_neutralized_returns(points)
    if len(rets) < _ROLLING_MIN_OBS:
        return []
    out: list[RollingRiskPoint] = []
    for j in range(_ROLLING_MIN_OBS, len(rets) + 1):
        window_len = min(j, _ROLLING_WINDOW)
        window = rets[j - window_len : j]
        end_pt, start_pt = points[j], points[j - window_len]
        elapsed = (end_pt.snap_date - start_pt.snap_date).days
        if elapsed < _MIN_ANNUALIZE_DAYS:
            continue
        ppy = window_len / (elapsed / 365.25)
        index = [1.0]
        for r in window:
            index.append(index[-1] * (1.0 + r))
        out.append(
            RollingRiskPoint(
                snap_date=end_pt.snap_date,
                volatility=volatility(window, periods_per_year=ppy),
                sharpe=sharpe_ratio(window, periods_per_year=ppy),
                sortino=sortino_ratio(window, periods_per_year=ppy),
                max_drawdown=max_drawdown(index),
            )
        )
    return out


def _account_perf_series(
    session: Session, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> tuple[list[PerfPoint], float | None]:
    """Aggregate the per-account NAV snapshots for ``account_ids`` into a subset NAV
    series (metron-ops#9). A day's subset NAV = the SUM of the selected accounts' rows;
    its flow = the sum of their flows; SPY close is shared.

    Apples-to-apples guard: the series begins only once EVERY account in the selection
    that ever reports has started (the latest of their first snapshot dates), and each
    included day requires all of them present. This way an account that starts later
    never reads as a spurious one-day NAV jump, and gap days are skipped. For a single
    account it's simply every date that account recorded."""
    rows = session.scalars(
        select(models.AccountNavSnapshot)
        .where(
            models.AccountNavSnapshot.tenant_id == tenant_id,
            models.AccountNavSnapshot.account_id.in_(list(account_ids)),
        )
        .order_by(models.AccountNavSnapshot.snap_date)
    ).all()
    if not rows:
        return [], None
    by_date: dict[date, list] = defaultdict(list)
    first_seen: dict[uuid.UUID, date] = {}
    for r in rows:
        by_date[r.snap_date].append(r)
        first_seen.setdefault(r.account_id, r.snap_date)

    # The cohort is the accounts that actually reported; the series can't start until the
    # last of them has data (else its arrival looks like a gain on an otherwise-flat day).
    cohort = set(first_seen)
    start = max(first_seen.values())
    points: list[PerfPoint] = []
    last_cost: float | None = None
    for d in sorted(by_date):
        if d < start:
            continue
        present = by_date[d]
        if {r.account_id for r in present} != cohort:
            continue  # a gap day for one of the accounts — skip rather than undercount
        nav = sum(float(r.nav) for r in present)
        flow = sum(float(r.external_flow) for r in present)
        spy = next((float(r.spy_close) for r in present if r.spy_close is not None), None)
        points.append(PerfPoint(snap_date=d, nav=nav, external_flow=flow, spy_close=spy))
        last_cost = sum(float(r.cost_basis) for r in present if r.cost_basis is not None)
    return points, last_cost


def performance(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    account_ids: Collection[uuid.UUID] | None = None,
) -> PerformanceSummary:
    """Performance metrics over the recorded snapshot series. Returns counts + None
    metrics until ≥2 snapshots exist.

    ``account_ids`` (a non-empty selection) scopes the series to those accounts' own
    forward-recorded ``AccountNavSnapshot`` rows (metron-ops#9) — per-account NAV can't be
    reconstructed for snapshot-sourced accounts, so this history accrues forward only.
    None / empty = the whole-portfolio ``NavSnapshot`` series (also reconstructable)."""
    if account_ids:
        points, last_cost_basis = _account_perf_series(session, tenant_id, account_ids)
    else:
        snaps = session.scalars(
            select(models.NavSnapshot)
            .where(models.NavSnapshot.tenant_id == tenant_id, models.NavSnapshot.portfolio_id == portfolio_id)
            .order_by(models.NavSnapshot.snap_date)
        ).all()
        points = [
            PerfPoint(
                snap_date=s.snap_date,
                nav=float(s.nav),
                external_flow=float(s.external_flow),
                spy_close=float(s.spy_close) if s.spy_close is not None else None,
            )
            for s in snaps
        ]
        last_cost_basis = float(snaps[-1].cost_basis) if snaps else None
    summary = PerformanceSummary(n_snapshots=len(points), points=points)
    summary.estimated, summary.estimated_note = nav_history_estimated(session, tenant_id, portfolio_id)
    if not points:
        return summary
    summary.first_date = points[0].snap_date
    summary.last_date = points[-1].snap_date
    summary.latest_nav = points[-1].nav
    summary.latest_cost_basis = last_cost_basis
    if len(points) < 2:
        return summary

    summary.days = (points[-1].snap_date - points[0].snap_date).days
    # Contributions after the first snapshot inflate end NAV without being performance.
    summary.net_contributions = sum(p.external_flow for p in points[1:])
    # Cumulative return = the flow-neutralized TOTAL (geometric link of the period returns).
    # The naive (last − contributions)/first divides by the FIRST NAV, which explodes when
    # the series starts from a tiny initial position later built up by contributions
    # (metron-ops#44 — that produced the +4972% cumulative). This coincides with the TWR.
    _cum = 1.0
    for r in _flow_neutralized_returns(points):
        _cum *= 1.0 + r
    summary.cumulative_return = _cum - 1.0
    # The lib wants each point's value BEFORE its flow; a snapshot's NAV is recorded
    # end-of-day (post-flow), so subtract the day's net deposit to recover the pre-flow
    # value. Then chaining end.value / (begin.value + begin.flow) neutralizes the flow.
    summary.twr = time_weighted_return(
        [ValuationPoint(when=p.snap_date, value=p.nav - p.external_flow, flow=p.external_flow) for p in points]
    )
    if summary.twr is not None and summary.days >= _MIN_ANNUALIZE_DAYS:
        summary.annualized_twr = annualize(summary.twr, summary.days)
    _apply_risk_and_alpha(summary, points)
    summary.rolling = _rolling_risk(points)
    return summary


def _window_twr(window: list[PerfPoint]) -> float | None:
    """Time-weighted return over a window anchored at ``window[0]`` — the geometric link of
    the flow-neutralized period returns (the same neutralization the headline TWR uses).
    None for a window of fewer than 2 points."""
    if len(window) < 2:
        return None
    cum = 1.0
    for r in _flow_neutralized_returns(window):
        cum *= 1.0 + r
    return cum - 1.0


def _window_base_index(points: list[PerfPoint], period: str, today: date) -> int | None:
    """Index of the ANCHOR point whose NAV is a window's starting value (the window is
    ``points[base:]``), or None when the series can't form the window.

    - ``today``: the prior recorded snapshot (the latest daily change).
    - ``ytd``: the last snapshot in a PRIOR year (year-end carry); if the series starts
      this year, its first point (return measured from the first recorded day of the year).
    - ``ltm``: the last snapshot on/before today−365d; if the series is shorter, its first
      point (return measured over all available history).
    The honest start_date the tile carries tells the user the actual span in the last two
    cases. Returns None if the anchor would be the final point (no window)."""
    n = len(points)
    if n < 2:
        return None
    end_i = n - 1
    if period == "today":
        base = end_i - 1
    elif period == "ytd":
        year = points[end_i].snap_date.year
        base = next((i for i in range(end_i, -1, -1) if points[i].snap_date.year < year), 0)
    elif period == "ltm":
        cutoff = today - timedelta(days=365)
        base = next((i for i in range(end_i, -1, -1) if points[i].snap_date <= cutoff), 0)
    else:
        return None
    return base if base < end_i else None


def _ensure_benchmark_coverage(
    session: Session, symbols: list[str], start: date, end: date, source: HistorySource | None
) -> None:
    """Backfill the benchmark close cache (price_bars) so it spans ``[start, end]`` before
    the tiles read it. Idempotent and network-light: skips the fetch when the cache already
    covers the window (earliest bar ≤ start AND latest bar within the weekend/holiday
    slack of end). Feed path only — the caller passes ``source`` only when entitled."""
    for sym in symbols:
        price_service.ensure_security(session, sym)
    hist = price_service.close_history_by_symbol(session, symbols)
    need = [
        sym
        for sym in symbols
        if (
            (series := hist.get(sym)) is None
            or series[0].bar_date > start
            or (end - series[-1].bar_date).days > _BENCH_COVERAGE_SLACK_DAYS
        )
    ]
    if need:
        price_service.backfill_prices(session, need, start, end, source=source)


def _load_perf_points(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_ids: Collection[uuid.UUID] | None,
) -> list[PerfPoint]:
    """The NAV snapshot series for the selection — per-account aggregate when scoped, else
    the whole-portfolio series (shared by ``performance`` and ``period_tiles``)."""
    if account_ids:
        return _account_perf_series(session, tenant_id, account_ids)[0]
    snaps = session.scalars(
        select(models.NavSnapshot)
        .where(models.NavSnapshot.tenant_id == tenant_id, models.NavSnapshot.portfolio_id == portfolio_id)
        .order_by(models.NavSnapshot.snap_date)
    ).all()
    return [
        PerfPoint(
            snap_date=s.snap_date,
            nav=float(s.nav),
            external_flow=float(s.external_flow),
            spy_close=float(s.spy_close) if s.spy_close is not None else None,
        )
        for s in snaps
    ]


def period_tiles(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    account_ids: Collection[uuid.UUID] | None = None,
    with_benchmarks: bool = True,
    benchmark_source: HistorySource | None = None,
) -> PeriodTilesResult:
    """Overview hero tiles (metron-ops#83): aggregate holdings performance over Today / YTD
    / LTM, each as $ investment gain + %TWR, plus the per-benchmark return and alpha.

    Benchmark comparison is FEED-GATED: ``with_benchmarks=False`` (the no-feed beta) yields
    portfolio-only tiles (no benchmark columns). When enabled, benchmark returns come from
    the price_bars close cache (SPY/QQQ/IWM ETF proxies); ``benchmark_source`` (passed only
    when feed-entitled) backfills any window the cache doesn't already span."""
    points = _load_perf_points(session, tenant_id, portfolio_id, account_ids)
    result = PeriodTilesResult(last_date=points[-1].snap_date if points else None)
    if len(points) < 2:
        return result

    # The anchor index per period, computed first so benchmark coverage spans only what the
    # earliest window actually needs (no over-fetch).
    bases = {p: _window_base_index(points, p, today) for p, _ in PERIOD_TILES}

    bench_history: dict[str, list[ClosePoint]] = {}
    if with_benchmarks:
        symbols = [s for s, _ in BENCHMARKS]
        formed = [b for b in bases.values() if b is not None]
        if formed:
            # benchmark_source=None resolves to the default spine source inside backfill;
            # coverage is a no-op (no network) when the cache already spans the window.
            _ensure_benchmark_coverage(
                session, symbols, points[min(formed)].snap_date, today, benchmark_source
            )
        bench_history = price_service.close_history_by_symbol(session, symbols)
        result.benchmarks_available = any(bench_history.get(s) for s in symbols)

    for period, label in PERIOD_TILES:
        base_i = bases[period]
        if base_i is None:
            result.tiles.append(PeriodTile(period, label, None, None, None, None, []))
            continue
        window = points[base_i:]
        base, end = window[0], window[-1]
        twr = _window_twr(window)
        gain = end.nav - base.nav - sum(p.external_flow for p in window[1:])
        benches: list[BenchmarkReturn] = []
        if with_benchmarks:
            for sym, blabel in BENCHMARKS:
                series = bench_history.get(sym)
                b_start = _asof_close(series, base.snap_date)
                b_end = _asof_close(series, end.snap_date)
                ret = (b_end / b_start - 1.0) if (b_start and b_end and b_start > 0) else None
                alpha = (twr - ret) if (twr is not None and ret is not None) else None
                benches.append(BenchmarkReturn(symbol=sym, label=blabel, ret=ret, alpha=alpha))
        result.tiles.append(PeriodTile(period, label, base.snap_date, end.snap_date, gain, twr, benches))
    return result


@dataclass
class SeriesPoint:
    when: date
    g: float  # cumulative growth from the first returned point (g[0] = 1.0)


@dataclass
class AccountSeries:
    account_id: uuid.UUID
    name: str
    points: list[SeriesPoint] = field(default_factory=list)


@dataclass
class BenchmarkSeries:
    symbol: str
    label: str
    points: list[SeriesPoint] = field(default_factory=list)


@dataclass
class HoldingsPerfSeries:
    """Per-account performance lines + benchmark overlays for the Holdings chart
    (metron-ops#78). Each series is a cumulative GROWTH index normalized to 1.0 at its
    first point, so the client can re-range and re-base to 100 without a refetch."""

    accounts: list[AccountSeries] = field(default_factory=list)
    benchmarks: list[BenchmarkSeries] = field(default_factory=list)
    benchmarks_available: bool = False


def _growth_index(points: list[PerfPoint]) -> list[SeriesPoint]:
    """Cumulative flow-neutralized growth over a snapshot series, g[0]=1.0. A day's growth
    factor is the flow-neutralized return (navₜ − flowₜ)/navₜ₋₁ — a deposit reads as capital
    in, never as a gain. A non-positive prior NAV contributes a flat day (no fabricated
    return)."""
    out = [SeriesPoint(when=points[0].snap_date, g=1.0)]
    g = 1.0
    for i in range(1, len(points)):
        prev = points[i - 1].nav
        r = (points[i].nav - points[i].external_flow) / prev - 1.0 if prev > 0 else 0.0
        g *= 1.0 + r
        out.append(SeriesPoint(when=points[i].snap_date, g=g))
    return out


def _benchmark_growth(series: list[ClosePoint] | None, start: date) -> list[SeriesPoint]:
    """Benchmark close history (on/after ``start``) as a growth index normalized to 1.0 at
    the first in-window close. Empty when the window has fewer than 2 closes."""
    if not series:
        return []
    window = [p for p in series if p.bar_date >= start and p.close > 0]
    if len(window) < 2:
        return []
    base = window[0].close
    return [SeriesPoint(when=p.bar_date, g=p.close / base) for p in window]


def account_performance_series(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    account_ids: Collection[uuid.UUID] | None = None,
    with_benchmarks: bool = True,
    benchmark_source: HistorySource | None = None,
) -> HoldingsPerfSeries:
    """Per-account performance lines for the Holdings chart (metron-ops#78): one cumulative
    flow-neutralized growth index per selected account (all accounts when the selection is
    empty), plus the SPY/QQQ/IWM benchmark overlays.

    Benchmark overlays are FEED-GATED (``with_benchmarks=False`` → no benchmark lines).
    Each series is normalized to 1.0 at its first point so the client re-ranges + re-bases
    to 100 itself. Per-account NAV can't be reconstructed (snapshot-sourced accounts report
    only current positions), so these accrue forward — short at first, filling in daily."""
    targets = list(account_ids) if account_ids else list(
        session.scalars(
            select(models.Account.id).where(models.Account.portfolio_id == portfolio_id)
        ).all()
    )
    if not targets:
        return HoldingsPerfSeries()
    names = {
        aid: (nickname or name or external_id)
        for aid, nickname, name, external_id in session.execute(
            select(models.Account.id, models.Account.nickname, models.Account.name, models.Account.external_id)
            .where(models.Account.id.in_(targets))
        ).all()
    }

    result = HoldingsPerfSeries()
    earliest: date | None = None
    for aid in targets:
        points = _account_perf_series(session, tenant_id, [aid])[0]
        if len(points) < 2:
            continue  # a single point isn't a line yet
        result.accounts.append(
            AccountSeries(account_id=aid, name=names.get(aid, str(aid)), points=_growth_index(points))
        )
        first = points[0].snap_date
        earliest = first if earliest is None else min(earliest, first)

    if with_benchmarks and earliest is not None:
        symbols = [s for s, _ in BENCHMARKS]
        _ensure_benchmark_coverage(session, symbols, earliest, today, benchmark_source)
        history = price_service.close_history_by_symbol(session, symbols)
        for sym, label in BENCHMARKS:
            pts = _benchmark_growth(history.get(sym), earliest)
            if pts:
                result.benchmarks.append(BenchmarkSeries(symbol=sym, label=label, points=pts))
        result.benchmarks_available = bool(result.benchmarks)
    return result


# --- historical reconstruction --------------------------------------------
#
# Forward-recording (record_snapshot) starts empty. Reconstruction seeds the series
# from history: backfill daily closes over the ledger span, then for a set of
# valuation dates replay the ledger to that date and value the positions held then at
# that date's close. Gives instant multi-year history where forward-recording would
# take years to accumulate.


def _asof_close(series: list[ClosePoint] | None, when: date) -> float | None:
    """Most recent close on or before ``when`` (carry-forward over non-trading days).
    ``series`` is ascending by date. None if nothing is on/before ``when``."""
    if not series:
        return None
    chosen: float | None = None
    for point in series:
        if point.bar_date <= when:
            chosen = point.close
        else:
            break
    return chosen


def _month_ends(start: date, end: date) -> list[date]:
    """Last calendar day of each month within ``[start, end]`` (inclusive)."""
    out: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last = nxt - timedelta(days=1)
        if start <= last <= end:
            out.append(last)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _valuation_dates(first: date, today: date, flow_dates: list[date]) -> list[date]:
    """Dates to value the portfolio at: month-ends (a smooth curve) + every external-flow
    date (so TWR sub-periods break cleanly on cash movements) + the endpoints."""
    dates = {first, today, *_month_ends(first, today), *(d for d in flow_dates if first <= d <= today)}
    return sorted(dates)


def _lot_account_ids(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> set[uuid.UUID]:
    """Accounts that provided lot-level open positions (e.g. IBKR Flex Lot detail)."""
    return set(
        session.scalars(
            select(models.OpenLot.account_id)
            .join(models.Account, models.OpenLot.account_id == models.Account.id)
            .where(models.OpenLot.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        ).all()
    )


def _load_lot_timeline(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID):
    """Per-ticker open + closed lots → the inputs to reconstruct the historical position.

    Returns ``(open_lots, closed_lots)`` where each is ``(ticker, qty, cost_basis,
    open_date[, close_date])``. An OPEN lot contributes to the position from its open date
    onward; a CLOSED lot (from ``realized_lots``) contributes between open and close
    (exclusive). Together they give position(ticker, d) without a replayable trade feed
    (metron-ops#74)."""
    open_lots = [
        (r.ticker, float(r.quantity), float(r.cost_basis), r.open_date)
        for r in session.scalars(
            select(models.OpenLot)
            .join(models.Account, models.OpenLot.account_id == models.Account.id)
            .where(models.OpenLot.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        ).all()
    ]
    closed_lots = [
        (r.ticker, float(r.quantity), float(r.cost_basis), r.open_date, r.close_date)
        for r in session.scalars(
            select(models.RealizedLot)
            .join(models.Account, models.RealizedLot.account_id == models.Account.id)
            .where(models.RealizedLot.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        ).all()
    ]
    return open_lots, closed_lots


def _lot_positions_asof(open_lots, closed_lots, when: date):
    """``(ticker → quantity, ticker → cost_basis)`` held as-of ``when`` from the lot
    timeline: an open lot counts once its open date has passed; a closed lot counts only
    between its open and close (exclusive). Pure — the core of lot-based reconstruction."""
    pos: dict[str, float] = defaultdict(float)
    cost: dict[str, float] = defaultdict(float)
    for ticker, qty, cb, od in open_lots:
        if od <= when:
            pos[ticker] += qty
            cost[ticker] += cb
    for ticker, qty, cb, od, cd in closed_lots:
        if od <= when < cd:
            pos[ticker] += qty
            cost[ticker] += cb
    return pos, cost


def _ticker_currencies(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> dict[str, str]:
    """ticker → native currency, from the lot tables — so historical native prices can be
    FX-converted to base before they enter NAV (a foreign close is not a USD value)."""
    out: dict[str, str] = {}
    for model in (models.OpenLot, models.RealizedLot):
        for ticker, ccy in session.execute(
            select(model.ticker, model.currency)
            .join(models.Account, model.account_id == models.Account.id)
            .where(model.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
        ).all():
            out.setdefault(ticker, ccy or "USD")
    return out


def _no_lot_flat_value(held, tickers_with_lots: set[str]) -> tuple[float, float, list[str]]:
    """Current base value + cost of holdings that have NO lots (e.g. bonds / money-market
    funds with no lot detail). Reconstruction can't place them on past dates, so they're
    carried FLAT-BACKWARD at current value rather than dropped — dropping undercounts the
    past and manufactures volatility (the metron-ops#44 lesson, applied to whole holdings)."""
    nav = cost = 0.0
    tickers: list[str] = []
    for h in held:
        if h.ticker in tickers_with_lots or h.market_value is None:
            continue
        nav += h.market_value
        cost += h.cost_basis_base or 0.0
        tickers.append(h.ticker)
    return nav, cost, tickers


def nav_history_estimated(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> tuple[bool, str | None]:
    """Is the reconstructed NAV history an ESTIMATE (incomplete lot coverage)?

    Exact only when every current holding's lots reconcile to its position quantity. A
    snapshot-sourced holding whose open-lot quantities don't sum to its current position
    (e.g. a SnapTrade/E*TRADE account with no lot detail, or partial activity history)
    can't have its past positions reconstructed → the series is an estimate, flagged with
    the affected tickers so the page can say so honestly (metron-ops#74)."""
    held = analytics.holdings(session, tenant_id, portfolio_id)
    if not held:
        return False, None
    lot_qty: dict[str, float] = defaultdict(float)
    for ticker, qty, _cb, _od in _load_lot_timeline(session, tenant_id, portfolio_id)[0]:
        lot_qty[ticker] += qty
    uncovered = [
        h.ticker for h in held
        if h.quantity > 0 and abs(lot_qty.get(h.ticker, 0.0) - h.quantity) > 0.01 * max(h.quantity, 1.0)
    ]
    if not uncovered:
        return False, None
    shown = ", ".join(sorted(uncovered)[:6]) + ("…" if len(uncovered) > 6 else "")
    return True, f"NAV history is estimated — incomplete lot data for {len(uncovered)} holding(s): {shown}."


def reconstruct_snapshots(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date, source: HistorySource | None = None
) -> int:
    """Seed the historical NAV series from the **lot timeline** + a ledger fallback.

    For snapshot-sourced accounts (IBKR/SnapTrade) there is no replayable trade feed, so
    the old transaction-replay reconstruction undercounted badly (the $419k vs $838k bug,
    metron-ops#74). Instead, derive position(ticker, d) from broker LOTS: open lots
    (``open_lots``, with open dates) contribute from their open date; closed lots
    (``realized_lots``) contribute between open and close. Accounts that provided NO lot
    data (CSV/OFX, or a snapshot source without lot detail) fall back to the transaction
    ledger replay. Each date is valued at the historical close (carry-forward), else the
    current price (flat) so a no-history fund stays in NAV. ``daily_refresh`` runs this
    BEFORE ``record_snapshot`` so the live-positions value (authoritative, and complete
    even for non-lot holdings) overwrites today's reconstructed point. Idempotent. Returns
    the number of snapshots written."""
    open_lots, closed_lots = _load_lot_timeline(session, tenant_id, portfolio_id)
    lot_account_ids = _lot_account_ids(session, tenant_id, portfolio_id)
    # Ledger from accounts WITHOUT lot data only — never double-count a lot-covered account.
    by_account = analytics.engine_transactions_by_account(session, tenant_id, portfolio_id)
    ledger_by_account = [(aid, t) for aid, t in by_account if aid not in lot_account_ids]
    ledger_txns = [t for _aid, t in ledger_by_account]

    if not open_lots and not closed_lots and not ledger_txns:
        return 0

    lot_dates = (
        [od for _t, _q, _cb, od in open_lots]
        + [od for _t, _q, _cb, od, _cd in closed_lots]
        + [cd for _t, _q, _cb, _od, cd in closed_lots]
    )
    first = min([*lot_dates, *(t.when for t in ledger_txns), today])
    symbols = sorted(
        {t for t, *_ in open_lots} | {t for t, *_ in closed_lots} | {t.ticker for t in ledger_txns if t.ticker}
    )

    price_service.ensure_security(session, "SPY")
    price_service.backfill_prices(session, [*symbols, "SPY"], first, today, source=source)
    history = price_service.close_history_by_symbol(session, [*symbols, "SPY"])
    spy_series = history.get("SPY")
    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    current_px = {h.ticker: h.last_price for h in held if h.last_price is not None}

    # Holdings covered by neither lots NOR the transaction ledger (e.g. a money-market
    # sweep with no lot detail and no trade feed) can't be placed historically → carry them
    # flat-backward at current base value rather than drop them (metron-ops#74). A holding
    # the ledger DOES value is excluded here so it isn't double-counted.
    covered = {t for t, *_ in open_lots}
    if ledger_txns:
        _full_ledger, _ = analytics.build_portfolio_ledger(ledger_by_account, log=False)
        covered |= {t for t in _full_ledger.open_lots if _full_ledger.position(t)[0] > 0}
    flat_nav, flat_cost, _flat_tickers = _no_lot_flat_value(held, covered)

    # FX: convert each ticker's NATIVE historical price to base USD at the rate as-of the
    # valuation date (a foreign close is not a USD value — the foreign-holding spike).
    ticker_ccy = _ticker_currencies(session, tenant_id, portfolio_id)
    foreign = sorted({c for c in ticker_ccy.values() if c and c != "USD"})
    if foreign:
        fx_service.backfill_fx_rates(session, foreign, first, today, base="USD")
    _rate_cache: dict[tuple[str, date], float] = {}

    def _rate(ccy: str, when: date) -> float:
        if not ccy or ccy == "USD":
            return 1.0
        key = (ccy, when)
        if key not in _rate_cache:
            _rate_cache[key] = (
                fx_service.rate_as_of(session, ccy, when)
                or fx_service.latest_rate_to_base(session, ccy)
                or 1.0
            )
        return _rate_cache[key]

    # Flows are cash deposits/withdrawals across ALL accounts (TWR sub-period breaks).
    flow_dates = [t.when for _aid, t in by_account if t.type in (TxnType.DEPOSIT, TxnType.WITHDRAWAL)]
    written = 0
    prev: date | None = None
    for when in _valuation_dates(first, today, flow_dates):
        pos, cost = _lot_positions_asof(open_lots, closed_lots, when)
        if ledger_txns:
            ledger, _incomplete = analytics.build_portfolio_ledger(
                [(aid, t) for aid, t in ledger_by_account if t.when <= when], log=False
            )
            for ticker in ledger.open_lots:
                shares, avg_cost = ledger.position(ticker)
                if shares > 0:
                    pos[ticker] += shares
                    cost[ticker] += shares * avg_cost

        nav = 0.0
        cost_basis = 0.0
        valued_any = False
        for ticker, shares in pos.items():
            if shares <= 0:
                continue
            cost_basis += cost[ticker]
            px = _asof_close(history.get(ticker), when) or current_px.get(ticker)
            if px is not None:
                nav += shares * px * _rate(ticker_ccy.get(ticker, "USD"), when)
                valued_any = True
        # No-lot holdings carried flat at current value (in addition to the lot-valued set).
        nav += flat_nav
        cost_basis += flat_cost
        if not valued_any and flat_nav == 0:
            continue
        flow = _net_purchases(session, tenant_id, portfolio_id, after=prev, through=when)
        _upsert_snapshot(
            session, tenant_id, portfolio_id, when,
            nav=nav, cost_basis=cost_basis, flow=flow, spy_close=_asof_close(spy_series, when),
        )
        written += 1
        prev = when
    session.commit()
    return written
