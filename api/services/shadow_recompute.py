"""Layer 3: independent shadow recomputation of NAV/TWR/realized P&L (metron-ops#218,
dashboard-accuracy epic metron-ops#210).

**The goal:** catch a bug that lives IN the production aggregation logic itself — e.g.
metron-ops#74's −96% drawdown bug (a sync race inflating NAV ~12x) or the per-account
TWR distortions of metron-ops#88/#89. A second call to the SAME code path would
faithfully reproduce the same bug; this module deliberately does NOT call
``performance.reconstruct_snapshots`` (or anything that shares its aggregation order) —
it recomputes from the same canonical transaction primitives via a genuinely different
route, then diffs the two answers.

**How production aggregates** (``api.services.performance.reconstruct_snapshots`` /
``_reconstruct_nav_points``): derives ``position(ticker, d)`` from the broker LOT
TIMELINE — ``analytics._load_lot_timeline`` builds per-account FIFO ledgers
(``analytics.build_portfolio_ledger``, lot relief scoped PER ACCOUNT), then walks each
lot's open/close date window independently per ticker, valuing each date from the price
cache. Snapshot-sourced accounts without lot detail are carried flat-backward, never
replayed. The rollup is: (per-account lots) -> (per-ticker date windows) -> (summed
into a NAV point per date).

**How this shadow module aggregates** (``shadow_nav_series``): a single forward
chronological REPLAY of the portfolio's ENTIRE merged transaction stream (every
account's transactions pooled together, not processed account-by-account) into ONE
``portfolio_analytics.domain.ledger.Ledger``, re-valued at every date a transaction
lands on. This differs from production in two structural ways, not just renamed
variables:

  1. **Aggregation order.** Production goes lots-first (build every account's full lot
     history, THEN slice by date per ticker, THEN sum across tickers/accounts into a
     date point). This shadow goes date-first (walk the merged event stream once in
     strict chronological order, growing one ledger, snapshotting NAV/realized at each
     date) — an event-sourced running-state replay instead of a
     derive-then-slice-then-roll-up.
  2. **Lot-matching scope.** Production's FIFO relief is scoped PER ACCOUNT (a SELL in
     one account can never close a lot bought in another — ``build_portfolio_ledger``'s
     explicit choice, "the IRS reality"). This shadow's FIFO relief is scoped PER
     PORTFOLIO (one merged ledger across every account). Production's own docstring
     calls per-account relief a discretionary choice — a bug in the per-account
     partitioning (a transaction misattributed to the wrong account) changes which lots
     close against which sells under per-account FIFO but is INVISIBLE to a
     portfolio-level FIFO relief over the same total transaction set (the merged ledger
     never looks at account_id at all). Conversely, a bug that double-counts a
     transaction across two ingestion sources distorts the shadow's merged total but can
     wash out under per-account relief if the duplicate lands entirely within one
     account's own lot count. Cross-checking both surfaces either failure mode; running
     the same order twice would catch neither.

Snapshot-sourced accounts without lot detail (IBKR Flex/SnapTrade's flat-backward
carry-forward in production) are, here, replayed like every other account — this shadow
does NOT special-case ``analytics._snapshot_sourced_account_ids``'s source split, so a
misclassification bug in that split does not silently propagate identically into both
numbers.

Consumes the SAME canonical input production does
(``analytics.engine_transactions`` — itself sourced from
``portfolio_analytics.ingestion`` / built on ``portfolio_analytics.domain.ledger``) so a
genuine divergence reflects the AGGREGATION LOGIC, not a different data source.
"""

from __future__ import annotations

import bisect
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.services import analytics, performance
from api.services import prices as price_service
from api.services.alerting import send_telegram_alert
from portfolio_analytics.domain.ledger import Ledger, Transaction, TxnType, build_ledger
from portfolio_analytics.prices.source import ClosePoint

logger = logging.getLogger(__name__)


# ── Shadow NAV/TWR/realized series ──────────────────────────────────────────────────


@dataclass
class ShadowPoint:
    """One date's shadow-recomputed figures: NAV (open-lot market value), cumulative
    realized P&L to date, and the BUY/SELL net flow ON that date (production's
    ``_purchase_flow`` sign convention: BUY +, SELL -) — the input a TWR needs to
    neutralize contributions the same way production's does."""

    when: date
    nav: float
    cumulative_realized: float
    flow: float


def _price_on_or_before(series: list[ClosePoint], when: date) -> float | None:
    """The latest cached close on or before ``when`` — ``bisect`` over the ascending
    ``close_history_by_symbol`` series (O(log n) per lookup vs. a linear scan)."""
    if not series:
        return None
    dates = [p.bar_date for p in series]
    i = bisect.bisect_right(dates, when) - 1
    return series[i].close if i >= 0 else None


def _position_market_value(
    ledger: Ledger, history: dict[str, list[ClosePoint]], when: date
) -> tuple[float, list[str]]:
    """Sum of ``shares * price_on_or_before(when)`` over every ticker with an open lot.
    Returns ``(value, unpriced_tickers)`` — a ticker with no cached close at/before
    ``when`` is excluded from the sum (never fabricated) and named in the second slot,
    the same "skip, don't fabricate" posture ``performance.record_snapshot`` uses."""
    total = 0.0
    unpriced: list[str] = []
    for ticker, lots in ledger.open_lots.items():
        shares = sum(lot.quantity for lot in lots)
        if shares <= 0:
            continue
        price = _price_on_or_before(history.get(ticker, []), when)
        if price is None:
            unpriced.append(ticker)
            continue
        total += shares * price
    return total, unpriced


def _txn_flow(txn: Transaction) -> float:
    """Net external-to-holdings flow for one transaction — mirrors
    ``performance._purchase_flow``'s BUY(+)/SELL(-) convention, applied to one
    ``Transaction`` instead of a batch of DB rows (this module works entirely off
    engine objects, never a second SQL round-trip for the flow)."""
    if txn.type is TxnType.BUY:
        return txn.quantity * txn.price if txn.price > 0 else txn.amount
    if txn.type is TxnType.SELL:
        return -(txn.quantity * txn.price)
    return 0.0


def shadow_nav_series(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, *, through: date
) -> tuple[list[ShadowPoint], list[str]]:
    """The shadow-recomputed NAV/realized series, one point per distinct transaction
    date up to and including ``through``. See the module docstring for how this
    aggregation differs structurally from ``performance.reconstruct_snapshots``.

    Returns ``(points, unpriced_tickers)`` — ``unpriced_tickers`` is the union of every
    ticker that was held but had no cached close at some point in the walk (that date's
    NAV under-counts by that leg's value; surfaced so the caller can decide whether to
    skip diffing an affected date rather than silently comparing a partial number).

    One portfolio-wide ``Ledger`` is grown forward across the WHOLE transaction stream
    (every account merged, in strict chronological order) — a single
    ``build_ledger(prefix)`` call per distinct date. This re-derives the full ledger
    from scratch at each date rather than mutating one ledger transaction-by-transaction
    (a straight-line single ``build_ledger`` pass would be O(n) instead of this
    function's O(n * dates)) — a deliberate simplicity-over-speed trade: the issue asks
    for "a simple reference implementation optimized for auditability over speed," and
    reusing the shared, already-tested ``build_ledger`` primitive unmodified at each cut
    point is far more auditable than a hand-rolled incremental FIFO mutator that
    duplicates its logic. A portfolio's real transaction count (dozens to low
    thousands) keeps this comfortably fast enough for a nightly batch job."""
    txns = analytics.engine_transactions(session, tenant_id, portfolio_id)
    txns = sorted((t for t in txns if t.when <= through), key=lambda t: t.when)
    if not txns:
        return [], []

    tickers = sorted({t.ticker for t in txns if t.ticker})
    history = price_service.close_history_by_symbol(session, tickers) if tickers else {}

    by_date: dict[date, list[Transaction]] = {}
    for t in txns:
        by_date.setdefault(t.when, []).append(t)
    dates_in_order = sorted(by_date)

    points: list[ShadowPoint] = []
    unpriced_all: set[str] = set()
    prefix: list[Transaction] = []
    for d in dates_in_order:
        day_flow = sum(_txn_flow(t) for t in by_date[d])
        prefix.extend(by_date[d])
        ledger = build_ledger(prefix)  # portfolio-wide FIFO relief — see module docstring
        cumulative_realized = sum(r.gain for r in ledger.realized)
        mv, unpriced = _position_market_value(ledger, history, d)
        unpriced_all.update(unpriced)
        points.append(ShadowPoint(when=d, nav=mv, cumulative_realized=cumulative_realized, flow=day_flow))

    return points, sorted(unpriced_all)


def shadow_twr(points: list[ShadowPoint]) -> float | None:
    """Cumulative time-weighted return over the shadow NAV series: the geometric link
    of per-period flow-neutralized returns, the same neutralization
    ``performance._flow_neutralized_returns`` applies (a point's NAV is recorded
    post-flow, so the period's flow is subtracted before dividing by the prior NAV).
    None when fewer than 2 points, or every prior NAV is non-positive."""
    if len(points) < 2:
        return None
    cum = 1.0
    any_period = False
    for i in range(1, len(points)):
        prev_nav = points[i - 1].nav
        if prev_nav <= 0:
            continue
        period_return = (points[i].nav - points[i].flow) / prev_nav - 1.0
        cum *= 1.0 + period_return
        any_period = True
    return cum - 1.0 if any_period else None


# ── Diff against production-served output ───────────────────────────────────────────


@dataclass
class Divergence:
    """One (portfolio, date, metric) divergence beyond tolerance."""

    portfolio_id: uuid.UUID
    as_of_date: date
    metric: str  # "nav" | "twr" | "realized_pnl"
    production_value: float | None
    shadow_value: float | None
    tolerance: float


def _abs_or_bps_tolerance(reference_value: float, *, floor_usd: float, bps: float) -> float:
    """Absolute-or-relative band, same construction as
    ``reconciliation._cost_basis_tolerance``: the larger of a flat $ floor and a
    bps-of-value band, so a small NAV isn't flagged for cents of price-cache staleness
    and a large one isn't flagged for a fraction-of-a-percent as-of-date rounding."""
    return max(floor_usd, abs(reference_value) * bps / 10_000)


def diff_nav_series(
    portfolio_id: uuid.UUID,
    production_points: list[tuple[date, float]],
    shadow_points: list[ShadowPoint],
    *,
    unpriced_dates: set[date] | None = None,
) -> list[Divergence]:
    """Diff production NAV (``(snap_date, nav)`` pairs, e.g. from ``models.NavSnapshot``)
    against the shadow series on shared dates. A date with an unpriced shadow leg
    (``unpriced_dates``) is skipped — that shadow NAV is known-incomplete, so comparing
    it would manufacture a false divergence, not a real one."""
    unpriced_dates = unpriced_dates or set()
    shadow_by_date = {p.when: p for p in shadow_points}
    out: list[Divergence] = []
    for when, prod_nav in production_points:
        if when in unpriced_dates:
            continue
        shadow = shadow_by_date.get(when)
        if shadow is None:
            continue
        tol = _abs_or_bps_tolerance(
            prod_nav, floor_usd=settings.shadow_recompute_nav_tolerance_usd,
            bps=settings.shadow_recompute_nav_tolerance_bps,
        )
        if abs(prod_nav - shadow.nav) > tol:
            out.append(Divergence(portfolio_id, when, "nav", prod_nav, shadow.nav, tol))
    return out


def diff_realized_pnl(
    portfolio_id: uuid.UUID,
    as_of: date,
    production_realized: float | None,
    shadow_realized: float | None,
) -> Divergence | None:
    """Diff production's cumulative realized P&L (as of ``as_of``) against the shadow's.
    None on either side means "not computed" — never diffed as a false 0 vs. real value.

    NOT YET WIRED into ``shadow_recompute_portfolio`` — production doesn't currently
    persist a standalone "realized P&L to date" figure anywhere this job can read
    without re-deriving it through the same lot-timeline aggregation this module exists
    to independently check (which would defeat the point). This function exists,
    exported and tested, so the wiring is a one-line addition once such a column lands
    (tracked as a follow-up in the PR description) rather than new logic written under
    time pressure later."""
    if production_realized is None or shadow_realized is None:
        return None
    reference = max(abs(production_realized), abs(shadow_realized), 1.0)
    tol = _abs_or_bps_tolerance(
        reference, floor_usd=settings.shadow_recompute_nav_tolerance_usd,
        bps=settings.shadow_recompute_nav_tolerance_bps,
    )
    if abs(production_realized - shadow_realized) > tol:
        return Divergence(portfolio_id, as_of, "realized_pnl", production_realized, shadow_realized, tol)
    return None


def diff_twr(
    portfolio_id: uuid.UUID,
    as_of: date,
    production_twr: float | None,
    shadow_twr_value: float | None,
) -> Divergence | None:
    """Diff production's cumulative TWR against the shadow's — a flat bp-of-return
    tolerance (TWR is already a %, so an absolute-or-relative $ band doesn't apply)."""
    if production_twr is None or shadow_twr_value is None:
        return None
    tol = settings.shadow_recompute_twr_tolerance_bps / 10_000
    if abs(production_twr - shadow_twr_value) > tol:
        return Divergence(portfolio_id, as_of, "twr", production_twr, shadow_twr_value, tol)
    return None


# ── Break persistence + alerting (mirrors api.services.reconciliation's shape) ──────


def _break_key(portfolio_id: uuid.UUID, metric: str, as_of: date) -> str:
    return f"{portfolio_id}:{metric}:{as_of.isoformat()}"


@dataclass
class ShadowRecomputeResult:
    portfolios_checked: int = 0
    breaks_open: int = 0
    breaks_new: int = 0
    breaks_resolved: int = 0
    errors: list[str] = field(default_factory=list)


def _persist_breaks(
    session: Session, tenant_id: uuid.UUID, divergences: list[Divergence], today: date
) -> tuple[list[models.ShadowRecomputeBreak], int]:
    """Upsert each divergence on its ``break_key`` (portfolio/metric/as_of-date).
    Returns (all rows touched, count newly opened or reopened — the alerting set)."""
    if not divergences:
        return [], 0
    keys = [_break_key(d.portfolio_id, d.metric, d.as_of_date) for d in divergences]
    existing = {
        row.break_key: row
        for row in session.scalars(
            select(models.ShadowRecomputeBreak).where(models.ShadowRecomputeBreak.break_key.in_(keys))
        ).all()
    }
    rows: list[models.ShadowRecomputeBreak] = []
    new_or_reopened = 0
    for d, key in zip(divergences, keys, strict=True):
        row = existing.get(key)
        if row is None:
            row = models.ShadowRecomputeBreak(
                tenant_id=tenant_id,
                portfolio_id=d.portfolio_id,
                break_type=d.metric,
                break_key=key,
                break_date=today,
                as_of_date=d.as_of_date,
                production_value=d.production_value,
                shadow_value=d.shadow_value,
                tolerance=d.tolerance,
            )
            session.add(row)
            new_or_reopened += 1
        else:
            row.production_value = d.production_value
            row.shadow_value = d.shadow_value
            row.tolerance = d.tolerance
            if row.resolved_at is not None:
                row.resolved_at = None
                row.alerted_at = None
                row.break_date = today
                new_or_reopened += 1
        rows.append(row)
    session.flush()
    return rows, new_or_reopened


def _resolve_stale_breaks(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, still_open_keys: set[str]
) -> int:
    """Mark resolved every unresolved break for this portfolio whose key did not
    reproduce this run (mirrors ``reconciliation._resolve_stale_breaks``)."""
    candidates = session.scalars(
        select(models.ShadowRecomputeBreak).where(
            models.ShadowRecomputeBreak.tenant_id == tenant_id,
            models.ShadowRecomputeBreak.portfolio_id == portfolio_id,
            models.ShadowRecomputeBreak.resolved_at.is_(None),
        )
    ).all()
    now = datetime.now(UTC)
    resolved = 0
    for row in candidates:
        if row.break_key not in still_open_keys:
            row.resolved_at = now
            resolved += 1
    return resolved


def _alert_text(portfolio: models.Portfolio, rows: list[models.ShadowRecomputeBreak]) -> str:
    lines = [f"Metron shadow-recompute: {len(rows)} new/reopened divergence(s) — portfolio {portfolio.name}"]
    for row in rows[:20]:
        lines.append(
            f"  [{row.break_type}] as_of={row.as_of_date} production={row.production_value} "
            f"shadow={row.shadow_value} tol={row.tolerance}"
        )
    if len(rows) > 20:
        lines.append(f"  … and {len(rows) - 20} more (see shadow_recompute_breaks table)")
    return "\n".join(lines)


def shadow_recompute_portfolio(
    session: Session, portfolio: models.Portfolio, *, today: date | None = None
) -> ShadowRecomputeResult:
    """Run layer-3 shadow recompute for one portfolio: build the shadow NAV/TWR/realized
    series, diff it against production's persisted ``NavSnapshot`` series (and latest
    realized P&L), upsert divergences, resolve stale ones, alert on anything new. Never
    raises on a per-portfolio failure — logs + alerts + records it in ``result.errors``,
    the same fail-loud-but-don't-crash-the-batch posture ``reconciliation.reconcile_all``
    uses for a broker-fetch failure."""
    today = today or datetime.now(UTC).date()
    result = ShadowRecomputeResult(portfolios_checked=1)
    try:
        shadow_points, unpriced_tickers = shadow_nav_series(
            session, portfolio.tenant_id, portfolio.id, through=today
        )
        prod_rows = session.execute(
            select(models.NavSnapshot.snap_date, models.NavSnapshot.nav).where(
                models.NavSnapshot.tenant_id == portfolio.tenant_id,
                models.NavSnapshot.portfolio_id == portfolio.id,
            )
        ).all()
        production_points = [(row[0], float(row[1])) for row in prod_rows]

        divergences = diff_nav_series(portfolio.id, production_points, shadow_points)

        # TWR: production's cumulative TWR over its OWN recorded NavSnapshot series
        # (api.services.performance.performance, the same call the Performance page
        # makes) vs. the shadow's cumulative TWR over its own series, as of the latest
        # date both sides actually cover. Both are CUMULATIVE-since-first-point figures,
        # so this is only a fair comparison when the two series share the same first
        # date (true for a portfolio recomputed through its whole history, as
        # ``shadow_nav_series`` does) — a portfolio whose NavSnapshot series starts later
        # than its transaction history (a mid-life onboarding) is skipped rather than
        # compared on a mismatched base, to avoid manufacturing a false divergence.
        if shadow_points and production_points:
            prod_first = min(d for d, _ in production_points)
            shadow_first = shadow_points[0].when
            latest_shared = min(shadow_points[-1].when, max(d for d, _ in production_points))
            if shadow_first == prod_first and latest_shared >= shadow_first:
                shadow_asof_points = [p for p in shadow_points if p.when <= latest_shared]
                shadow_twr_value = shadow_twr(shadow_asof_points)
                prod_summary = performance.performance(session, portfolio.tenant_id, portfolio.id)
                twr_divergence = diff_twr(portfolio.id, latest_shared, prod_summary.twr, shadow_twr_value)
                if twr_divergence is not None:
                    divergences.append(twr_divergence)

                # Realized P&L: production doesn't persist a standalone "realized to
                # date" figure on NavSnapshot (nav/cost_basis together approximate
                # UNREALIZED only), so there is no independently-stored production value
                # to diff the shadow's cumulative realized against yet without re-deriving
                # it through the same lot-timeline path this module exists to avoid
                # trusting blindly. Tracked as a follow-up once layer 1/2 (or this layer)
                # adds a persisted realized-to-date column — noted in the PR description,
                # not silently dropped.

        rows, new_count = _persist_breaks(session, portfolio.tenant_id, divergences, today)
        still_open_keys = {row.break_key for row in rows}
        resolved_count = _resolve_stale_breaks(session, portfolio.tenant_id, portfolio.id, still_open_keys)
        session.commit()

        result.breaks_open = len(rows)
        result.breaks_new = new_count
        result.breaks_resolved = resolved_count

        if unpriced_tickers:
            logger.warning(
                "shadow-recompute: portfolio=%s had unpriced ticker(s) at some date(s) in the walk "
                "(NAV under-counts on those dates, excluded from diffing): %s",
                portfolio.id, unpriced_tickers,
            )

        if new_count:
            new_rows = [r for r in rows if r.alerted_at is None]
            if send_telegram_alert(_alert_text(portfolio, new_rows)):
                now = datetime.now(UTC)
                for row in new_rows:
                    row.alerted_at = now
                session.commit()
    except Exception as e:  # noqa: BLE001 — a per-portfolio failure must alert, never crash the batch
        msg = f"shadow-recompute failed — portfolio={portfolio.id}: {e}"
        logger.error(msg, exc_info=True)
        send_telegram_alert(f"⚠️ {msg}")
        result.errors.append(msg)
        session.rollback()

    return result


def shadow_recompute_all(session: Session) -> ShadowRecomputeResult:
    """Shadow-recompute every non-reference portfolio — the nightly job's entrypoint.
    Excludes the Showcase Portfolio for the same reason ``daily_refresh`` does
    (``api.maintenance``'s ``is_reference_rate`` branch, metron-ops#141): its NAV series
    is sole-sourced from the engine's published artifact, not from Metron's own
    transaction ledger, so there is nothing for either the production OR the shadow path
    to independently recompute."""
    from api.services.demo import REFERENCE_PORTFOLIO_ID

    total = ShadowRecomputeResult()
    portfolios = list(
        session.scalars(select(models.Portfolio).where(models.Portfolio.id != REFERENCE_PORTFOLIO_ID)).all()
    )
    for p in portfolios:
        r = shadow_recompute_portfolio(session, p)
        total.portfolios_checked += r.portfolios_checked
        total.breaks_open += r.breaks_open
        total.breaks_new += r.breaks_new
        total.breaks_resolved += r.breaks_resolved
        total.errors.extend(r.errors)
    return total
