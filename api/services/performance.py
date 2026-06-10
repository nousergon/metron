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

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from alpha_engine_lib.quant.returns import ValuationPoint, annualize, cumulative_return, time_weighted_return
from alpha_engine_lib.quant.riskstats import max_drawdown, sharpe_ratio, sortino_ratio, volatility
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import TxnType, build_ledger
from portfolio_analytics.prices import ClosePoint, HistorySource, fetch_latest_closes


def _external_flow_on(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, when: date) -> float:
    """Net external cash flow into the portfolio on ``when`` (deposits +, withdrawals −).

    BUY/SELL/DIVIDEND/FEE are internal to the portfolio and are NOT external flows;
    only DEPOSIT/WITHDRAWAL move capital across the portfolio boundary."""
    rows = session.execute(
        select(models.Transaction.txn_type, models.Transaction.amount)
        .join(models.Account, models.Transaction.account_id == models.Account.id)
        .where(
            models.Transaction.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
            models.Transaction.trade_date == when,
            models.Transaction.txn_type.in_([TxnType.DEPOSIT.value, TxnType.WITHDRAWAL.value]),
        )
    ).all()
    flow = 0.0
    for txn_type, amount in rows:
        flow += float(amount) if txn_type == TxnType.DEPOSIT.value else -float(amount)
    return flow


def record_snapshot(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date, source=None
) -> models.NavSnapshot | None:
    """Record today's NAV snapshot (idempotent per day). Returns the row, or None when
    NAV isn't computable yet (no holding has a cached price → nothing to value)."""
    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    priced = [h for h in held if h.market_value is not None]
    if not priced:
        return None  # can't snapshot a NAV we can't value — never fabricate one
    nav = sum(h.market_value for h in priced)
    cost_basis = sum(h.cost_basis for h in held)
    flow = _external_flow_on(session, tenant_id, portfolio_id, today)
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


@dataclass
class PerfPoint:
    snap_date: date
    nav: float
    external_flow: float
    spy_close: float | None


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
    points: list[PerfPoint] = field(default_factory=list)


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
    the irregular snapshot cadence rather than assuming 252 trading days."""
    rets = _flow_neutralized_returns(points)
    if rets:
        # Growth index of the flow-neutralized returns → honest peak-to-trough drawdown.
        index = [1.0]
        for r in rets:
            index.append(index[-1] * (1.0 + r))
        summary.max_drawdown = max_drawdown(index)
        years = summary.days / 365.25 if summary.days > 0 else 0.0
        ppy = len(rets) / years if years > 0 else 252.0
        summary.volatility = volatility(rets, periods_per_year=ppy)
        summary.sharpe = sharpe_ratio(rets, periods_per_year=ppy)
        summary.sortino = sortino_ratio(rets, periods_per_year=ppy)

    spy = [p.spy_close for p in points if p.spy_close is not None]
    if len(spy) >= 2 and spy[0] > 0:
        summary.spy_return = spy[-1] / spy[0] - 1.0
        port_return = summary.twr if summary.twr is not None else summary.cumulative_return
        if port_return is not None:
            summary.alpha = port_return - summary.spy_return


def performance(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> PerformanceSummary:
    """Performance metrics over the recorded snapshot series. Returns counts + None
    metrics until ≥2 snapshots exist."""
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
    summary = PerformanceSummary(n_snapshots=len(points), points=points)
    if not points:
        return summary
    summary.first_date = points[0].snap_date
    summary.last_date = points[-1].snap_date
    summary.latest_nav = points[-1].nav
    summary.latest_cost_basis = float(snaps[-1].cost_basis)
    if len(points) < 2:
        return summary

    summary.days = (points[-1].snap_date - points[0].snap_date).days
    # Contributions after the first snapshot inflate end NAV without being performance.
    summary.net_contributions = sum(p.external_flow for p in points[1:])
    summary.cumulative_return = cumulative_return(
        points[0].nav, points[-1].nav, net_contributions=summary.net_contributions
    )
    # The lib wants each point's value BEFORE its flow; a snapshot's NAV is recorded
    # end-of-day (post-flow), so subtract the day's net deposit to recover the pre-flow
    # value. Then chaining end.value / (begin.value + begin.flow) neutralizes the flow.
    summary.twr = time_weighted_return(
        [ValuationPoint(when=p.snap_date, value=p.nav - p.external_flow, flow=p.external_flow) for p in points]
    )
    if summary.twr is not None and summary.days > 0:
        summary.annualized_twr = annualize(summary.twr, summary.days)
    _apply_risk_and_alpha(summary, points)
    return summary


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


def reconstruct_snapshots(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, today: date, source: HistorySource | None = None
) -> int:
    """Seed the NAV snapshot series from history: backfill daily closes over the ledger
    span, then value the portfolio at each valuation date by replaying the ledger to
    that date. Idempotent (upserts per day). Returns the number of snapshots written.

    A position whose ticker has no cached history on a date is excluded from that date's
    NAV (never fabricated); a date with nothing priced is skipped entirely."""
    txns = analytics.engine_transactions(session, tenant_id, portfolio_id)
    if not txns:
        return 0
    first = min(t.when for t in txns)
    symbols = sorted({t.ticker for t in txns if t.ticker})

    # Cache a SPY security so its history backfills for the benchmark, then backfill all.
    price_service.ensure_security(session, "SPY")
    price_service.backfill_prices(session, [*symbols, "SPY"], first, today, source=source)
    history = price_service.close_history_by_symbol(session, [*symbols, "SPY"])
    spy_series = history.get("SPY")

    flow_dates = [t.when for t in txns if t.type in (TxnType.DEPOSIT, TxnType.WITHDRAWAL)]
    written = 0
    for when in _valuation_dates(first, today, flow_dates):
        ledger = build_ledger([t for t in txns if t.when <= when])
        nav = 0.0
        cost_basis = 0.0
        valued_any = False
        for ticker in ledger.open_lots:
            shares, avg_cost = ledger.position(ticker)
            if shares <= 0:
                continue
            cost_basis += shares * avg_cost
            px = _asof_close(history.get(ticker), when)
            if px is not None:
                nav += shares * px
                valued_any = True
        if not valued_any:
            continue  # nothing priced as-of this date → no fabricated NAV
        flow = _external_flow_on(session, tenant_id, portfolio_id, when)
        _upsert_snapshot(
            session, tenant_id, portfolio_id, when,
            nav=nav, cost_basis=cost_basis, flow=flow, spy_close=_asof_close(spy_series, when),
        )
        written += 1
    session.commit()
    return written
