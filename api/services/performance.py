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
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.prices import ClosePoint, HistorySource, fetch_latest_closes

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
    # Base-currency cost basis (matches the base-currency NAV); a holding with no cached
    # FX rate is excluded rather than summed at native face value.
    cost_basis = sum(h.cost_basis_base for h in held if h.cost_basis_base is not None)
    flow = _net_purchases(
        session, tenant_id, portfolio_id,
        after=_last_snapshot_date(session, tenant_id, portfolio_id, today), through=today,
    )
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

    A position with NO price history at all (a fund the data spine can't price — e.g. a
    Fidelity ZERO fund) is valued at its CURRENT price (flat) rather than dropped: dropping
    it made the reconstructed NAV diverge badly from the live market value (the $374k vs
    $835k bug) and manufactured spurious volatility/drawdown (metron-ops#44). A position
    with no historical AND no current price is the only one excluded; a date with nothing
    valuable is skipped."""
    txns_by_account = analytics.engine_transactions_by_account(session, tenant_id, portfolio_id)
    txns = [t for _aid, t in txns_by_account]
    if not txns:
        return 0
    first = min(t.when for t in txns)
    symbols = sorted({t.ticker for t in txns if t.ticker})

    # Cache a SPY security so its history backfills for the benchmark, then backfill all.
    price_service.ensure_security(session, "SPY")
    price_service.backfill_prices(session, [*symbols, "SPY"], first, today, source=source)
    history = price_service.close_history_by_symbol(session, [*symbols, "SPY"])
    spy_series = history.get("SPY")

    # Current per-share price per held ticker (broker / latest cache) — the flat fallback
    # for a holding the historical spine can't price, so it stays in NAV every date.
    current_px = {
        h.ticker: h.last_price
        for h in analytics.valued_holdings(session, tenant_id, portfolio_id)
        if h.last_price is not None
    }

    # WARN once, over the full set, for any per-(account, ticker) group whose history
    # can't replay; the per-date loop below then skips quietly (log=False) instead of
    # repeating the same warning for every valuation date.
    analytics.build_portfolio_ledger(txns_by_account)

    flow_dates = [t.when for t in txns if t.type in (TxnType.DEPOSIT, TxnType.WITHDRAWAL)]
    written = 0
    prev: date | None = None
    for when in _valuation_dates(first, today, flow_dates):
        ledger, _incomplete = analytics.build_portfolio_ledger(
            [(aid, t) for aid, t in txns_by_account if t.when <= when], log=False
        )
        nav = 0.0
        cost_basis = 0.0
        valued_any = False
        for ticker in ledger.open_lots:
            shares, avg_cost = ledger.position(ticker)
            if shares <= 0:
                continue
            cost_basis += shares * avg_cost
            # Historical close (carry-forward); else the current price (flat) so a
            # no-history fund stays in NAV instead of dropping out (metron-ops#44).
            px = _asof_close(history.get(ticker), when)
            if px is None:
                px = current_px.get(ticker)
            if px is not None:
                nav += shares * px
                valued_any = True
        if not valued_any:
            continue  # nothing valuable as-of this date (no historical or current price)
        # Net purchases since the prior valued snapshot — the contributions to neutralize.
        # (Per-period, so every buy between snapshots is captured without needing each buy
        # date as a valuation date.) (metron-ops#44)
        flow = _net_purchases(session, tenant_id, portfolio_id, after=prev, through=when)
        _upsert_snapshot(
            session, tenant_id, portfolio_id, when,
            nav=nav, cost_basis=cost_basis, flow=flow, spy_close=_asof_close(spy_series, when),
        )
        written += 1
        prev = when
    session.commit()
    return written
