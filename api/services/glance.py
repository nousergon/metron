"""The glance screen, composed server-side in one round trip (metron-ops#248 Stage A, #250, #214).

One phone screen, six zones: two fixed (headline, path), three ranked (movers, insights,
ahead) and one fixed-and-never-silent (integrity). This module is the **composition
layer only** — every number comes from a service the analytics pages already use
(``analytics.summary``, ``performance.period_tiles``, ``tax.tax_lots``,
``intraday.today_view``, ``calendar.upcoming_events``, the NAV snapshot series and the
reconciliation tables). A parallel computation here would be a second source of truth
for numbers another page already shows, and the two would disagree in front of a user.

## How the ranked zones are filled

Zones 3–5 are slots filled by the deterministic ranker (``api.insights.ranker``) from the
facet catalog (``api.insights.registry``); they are never a widget per facet. A facet
reaches the screen when:

1. ``registry.candidate_facets`` admits it for the request's tier and feed toggle (L1
   only — a directive facet can never reach this surface), and
2. a **producer** is registered for its key in ``PRODUCERS`` (``register_producer``).

Each producer turns existing service output into ``ranker.Observation`` rows carrying an
``as_of``. The zone a facet renders in is derived from its family (``zone_for``), so a
newly registered facet — any family — renders without a change to this module or the
web client. A facet with a producer that the tier or feed excludes is listed by label in
its zone's ``unavailable`` field: a feed-derived fact degrades to "not available", never
to a fabricated or silently missing value.

## Provenance

Nothing is unprovenanced (positioning §3g.4 law 4). Every item carries ``as_of`` and a
``provenance`` of ``live`` (delayed intraday quote), ``settled`` (close-to-close from the
NAV snapshot series), ``as_of_sync`` (the broker's last holdings sync) or ``as_of_close``
(the last cached close). Provenance is per row, not per page — this surface reports
current state (§3g.2).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from krepis.trading_calendar import is_market_hours, is_trading_day
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models
from api.insights import ranker, registry
from api.services import analytics, calendar, intraday, performance, reconciliation, tax
from portfolio_analytics.domain.diagnostics import DiagnosticsPosition, compute_diagnostics
from portfolio_analytics.domain.tax import LONG_TERM_DAYS, SHORT_TERM

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ── Render states (positioning §3g.4 law 5) — a payload field, never inferred by the client.
STATE_PRE_OPEN = "pre_open"
STATE_OPEN = "open"
STATE_POST_CLOSE = "post_close"

# ── Per-row provenance.
PROV_LIVE = "live"
PROV_SETTLED = "settled"
PROV_AS_OF_SYNC = "as_of_sync"
PROV_AS_OF_CLOSE = "as_of_close"

# ── Ranked zones.
ZONE_MOVERS = "movers"
ZONE_INSIGHTS = "insights"
ZONE_AHEAD = "ahead"

ZONE_LIMITS: dict[str, int] = {ZONE_MOVERS: 3, ZONE_INSIGHTS: 5, ZONE_AHEAD: 3}

# The quiet-day floor copy per zone. Insights uses the ranker's own floor statement; the
# other two say plainly what "nothing" means for that zone rather than padding slots.
MOVERS_FLOOR_TEXT = "No single position moved the portfolio materially in this session."
AHEAD_FLOOR_TEXT = "Nothing dated touches your holdings in the coming weeks."
FLOOR_TEXT_BY_ZONE: dict[str, str] = {
    ZONE_MOVERS: MOVERS_FLOOR_TEXT,
    ZONE_INSIGHTS: ranker.FLOOR_TEXT,
    ZONE_AHEAD: AHEAD_FLOOR_TEXT,
}

# Family → zone. A family not listed renders in insights, which is what lets a facet from
# a family this module has never heard of reach the screen without an edit here.
_ZONE_BY_FAMILY: dict[str, str] = {"movement": ZONE_MOVERS, "events": ZONE_AHEAD}
# Facet-level exceptions: a lot crossing the one-year boundary is a dated thing ahead
# (§3g.4 zone 5), even though its family is tax.
_ZONE_BY_FACET: dict[str, str] = {"long_term_boundary": ZONE_AHEAD}

# Horizons for the "ahead" producers.
LONG_TERM_HORIZON_DAYS = 30
EARNINGS_HORIZON_DAYS = 14

_BROKER_FEEDS = frozenset({"ibkr_flex", "snaptrade"})


def zone_for(facet: registry.Facet) -> str:
    """The ranked zone a facet renders in."""
    return _ZONE_BY_FACET.get(facet.key) or _ZONE_BY_FAMILY.get(facet.family) or ZONE_INSIGHTS


def render_state(now: datetime) -> str:
    """``open`` inside a regular NYSE session; ``pre_open`` on a trading day before the
    open; ``post_close`` otherwise (after the close, weekends and holidays — the settled
    view of the last completed session)."""
    if is_market_hours(now):
        return STATE_OPEN
    et = now.astimezone(_ET)
    if is_trading_day(et.date()) and et.time() < dtime(9, 30):
        return STATE_PRE_OPEN
    return STATE_POST_CLOSE


# ── Payload ───────────────────────────────────────────────────────────────────


@dataclass
class Headline:
    """Zone 1. Value as of the last holdings sync (daily is the shipping baseline, ruled
    2026-07-30), with the day's change carrying its own provenance."""

    base_currency: str
    total_value: float | None
    market_value: float | None
    cash: float | None
    value_as_of: str | None
    value_provenance: str
    last_sync: str | None
    day_change: float | None
    day_pct: float | None
    day_as_of: str | None
    day_provenance: str | None
    day_note: str | None
    n_accounts: int
    n_brokers: int
    surface: str = "overview"


@dataclass
class PathPoint:
    date: str
    nav: float


@dataclass
class PathZone:
    """Zone 2 — the settled NAV path. The client cycles the period over these points."""

    available: bool
    reason: str | None
    points: list[PathPoint]
    as_of: str | None
    provenance: str = PROV_SETTLED
    surface: str = "performance"


@dataclass
class GlanceItem:
    facet_key: str
    family: str
    label: str
    text: str
    as_of: str
    provenance: str
    surface: str
    score: float
    ticker: str | None = None
    amount: float | None = None
    pct: float | None = None
    event_date: str | None = None


@dataclass
class RankedZone:
    key: str
    items: list[GlanceItem]
    is_floor: bool
    floor_text: str | None
    as_of: str
    # Labels of facets this zone could show that the tier or feed excludes — rendered as
    # "not available", never hidden and never fabricated.
    unavailable: list[str] = field(default_factory=list)


@dataclass
class IntegrityZone:
    """Zone 6 — always rendered, healthy path included (§3g.4 law 3)."""

    status: str
    healthy: bool
    text: str
    open_breaks: int
    last_reconciled_at: str | None
    positions_as_of: str | None
    brokers: list[str]
    as_of: str
    surface: str = "diagnostics"


@dataclass
class Coverage:
    """How much of the catalog this deployment actually produced — so a thin screen is
    measurable rather than mistaken for a quiet day."""

    candidate_facets: int
    produced_facets: int
    observations: int


@dataclass
class GlanceScreen:
    portfolio_id: str
    generated_at: str
    state: str
    tier: str
    feed_enabled: bool
    headline: Headline
    path: PathZone
    movers: RankedZone
    insights: RankedZone
    ahead: RankedZone
    integrity: IntegrityZone
    coverage: Coverage
    # Producers that raised this request (facet keys). Non-empty means the ranked zones
    # are incomplete; the error itself is logged at ERROR with its traceback.
    degraded: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


# ── Producer seam ───────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """An observation plus the structured fields the zone renders beside its text."""

    observation: ranker.Observation
    provenance: str
    ticker: str | None = None
    amount: float | None = None
    pct: float | None = None
    event_date: date | None = None


@dataclass
class GlanceContext:
    """Shared inputs, computed once per request and handed to every producer."""

    session: Session
    portfolio: models.Portfolio
    now: datetime
    today: date
    tier: str
    feed_enabled: bool
    held: list[analytics.Holding]
    summary: analytics.PortfolioSummary
    total_value: float | None
    value_as_of: str
    today_tile: performance.PeriodTile | None
    today_view: intraday.TodaySummary | None
    quotes_reader: Callable[[], dict | None] | None = None
    _memo: dict = field(default_factory=dict)

    def tax_summary(self) -> tax.TaxSummary:
        if "tax" not in self._memo:
            self._memo["tax"] = tax.tax_lots(
                self.session, self.portfolio.tenant_id, self.portfolio.id, today=self.today, taxable_only=True
            )
        return self._memo["tax"]

    def weight_of(self, ticker: str) -> float:
        if not self.total_value:
            return 0.0
        mv = sum(h.market_value or 0.0 for h in self.held if h.ticker == ticker)
        return max(0.0, mv) / self.total_value


Producer = Callable[[GlanceContext], list[Candidate]]

PRODUCERS: dict[str, Producer] = {}


def register_producer(facet_key: str) -> Callable[[Producer], Producer]:
    """Register ``fn`` as the producer for a catalog facet. Raises on a key the registry
    does not know — a producer for an unregistered facet could never pass
    ``candidate_facets`` and would silently never render."""

    def _decorate(fn: Producer) -> Producer:
        if facet_key not in registry.FACET_BY_KEY:
            raise ValueError(f"no registered facet {facet_key!r}; register it in api.insights.registry first")
        if facet_key in PRODUCERS:
            raise ValueError(f"facet {facet_key!r} already has a producer")
        PRODUCERS[facet_key] = fn
        return fn

    return _decorate


def _scale(value: float, full: float) -> float:
    """Map ``value`` onto 0..1 where ``full`` (and anything above) is 1."""
    if full <= 0:
        raise ValueError("full must be positive")
    return max(0.0, min(1.0, value / full))


def _money(value: float, currency: str) -> str:
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    body = f"{abs(value):,.0f}"
    return f"{sign}${body}" if currency == "USD" else f"{sign}{body} {currency}"


def _unsigned_money(value: float, currency: str) -> str:
    return _money(abs(value), currency).lstrip("+")


# ── Producers (L1 observations only; wording describes, never directs) ───────


@register_producer("movement_decomposition")
def _movers(ctx: GlanceContext) -> list[Candidate]:
    """Positions by DOLLAR contribution to the day (§3g.4 zone 3), not percent move.

    Live when the delayed intraday decomposition is applied and fresh
    (``intraday.today_view``); otherwise settled, from the per-holding legs persisted on
    the two NAV snapshots that bound the TODAY tile (``performance.period_tiles``) — the
    same window the Overview tile reports.
    """
    ccy = ctx.summary.base_currency
    tv = ctx.today_view
    out: list[Candidate] = []
    if tv is not None and tv.available and not tv.stale and tv.rows and tv.as_of_utc:
        denom = tv.covered_prev_mv or ctx.total_value
        for r in tv.rows:
            if r.day_gain is None or not denom:
                continue
            out.append(
                Candidate(
                    ranker.Observation(
                        facet_key="movement_decomposition",
                        text=f"{r.ticker} moved the portfolio {_money(r.day_gain, ccy)} today.",
                        as_of=tv.as_of_utc,
                        materiality=_scale(abs(r.day_gain) / denom, 0.01),
                    ),
                    provenance=PROV_LIVE,
                    ticker=r.ticker,
                    amount=r.day_gain,
                    pct=r.day_pct,
                )
            )
        return out

    tile = ctx.today_tile
    if tile is None or tile.start_date is None or tile.end_date is None:
        return out
    snaps = {
        s.snap_date: s
        for s in ctx.session.scalars(
            select(models.NavSnapshot).where(
                models.NavSnapshot.tenant_id == ctx.portfolio.tenant_id,
                models.NavSnapshot.portfolio_id == ctx.portfolio.id,
                models.NavSnapshot.snap_date.in_([tile.start_date, tile.end_date]),
            )
        ).all()
    }
    start, end = snaps.get(tile.start_date), snaps.get(tile.end_date)
    if start is None or end is None or not start.composition or not end.composition:
        return out
    prev_legs = {leg["ticker"]: leg for leg in start.composition.get("legs", [])}
    nav = float(start.nav) or ctx.total_value
    as_of = tile.end_date.isoformat()
    for leg in end.composition.get("legs", []):
        prev = prev_legs.get(leg["ticker"])
        if prev is None or not nav:
            continue
        p0, p1, qty, fx = prev.get("price"), leg.get("price"), leg.get("qty"), leg.get("fx_rate")
        if p0 in (None, 0) or p1 is None or qty is None or fx is None:
            continue
        # The price-move component at the closing quantity — the settled analogue of
        # TodayRow.day_gain (qty × (last − prev_close) × fx). A quantity change between the
        # two snapshots is a trade or a flow, not a move, and is deliberately excluded.
        contribution = float(qty) * (float(p1) - float(p0)) * float(fx)
        out.append(
            Candidate(
                ranker.Observation(
                    facet_key="movement_decomposition",
                    text=f"{leg['ticker']} moved the portfolio {_money(contribution, ccy)} in the session ending {as_of}.",
                    as_of=as_of,
                    materiality=_scale(abs(contribution) / nav, 0.01),
                ),
                provenance=PROV_SETTLED,
                ticker=leg["ticker"],
                amount=contribution,
                pct=float(p1) / float(p0) - 1.0,
            )
        )
    return out


@register_producer("concentration_top_weight")
def _top_weight(ctx: GlanceContext) -> list[Candidate]:
    """The largest position's share of invested market value, from the diagnostics engine."""
    positions = [
        DiagnosticsPosition(ticker=h.ticker, market_value=h.market_value)
        for h in ctx.held
        if h.security_type != "cash" and h.market_value is not None and h.market_value > 0
    ]
    if not positions:
        return []
    conc = compute_diagnostics(positions).concentration
    if conc is None:
        return []
    return [
        Candidate(
            ranker.Observation(
                facet_key="concentration_top_weight",
                text=f"{conc.max_position_ticker} is {conc.max_position_weight:.1%} of invested market value, the largest position.",
                as_of=ctx.value_as_of,
                materiality=_scale(conc.max_position_weight, 0.5),
            ),
            provenance=PROV_AS_OF_CLOSE,
            ticker=conc.max_position_ticker,
            pct=conc.max_position_weight,
        )
    ]


@register_producer("cash_drag")
def _cash(ctx: GlanceContext) -> list[Candidate]:
    cash = ctx.summary.cash
    if cash is None or cash <= 0 or not ctx.total_value:
        return []
    share = cash / ctx.total_value
    return [
        Candidate(
            ranker.Observation(
                facet_key="cash_drag",
                text=f"Uninvested cash is {share:.1%} of the portfolio ({_unsigned_money(cash, ctx.summary.base_currency)}).",
                as_of=ctx.value_as_of,
                materiality=_scale(share, 0.2),
            ),
            provenance=PROV_AS_OF_SYNC,
            amount=cash,
            pct=share,
        )
    ]


@register_producer("harvestable_losses_present")
def _losses(ctx: GlanceContext) -> list[Candidate]:
    """Descriptive only: that losses exist. Naming which to sell is F8 and L2."""
    summary = ctx.tax_summary()
    loss = summary.harvestable_loss
    if not loss or not ctx.total_value:
        return []
    return [
        Candidate(
            ranker.Observation(
                facet_key="harvestable_losses_present",
                text=f"Positions in taxable accounts carry {_unsigned_money(loss, summary.base_currency)} of unrealized losses.",
                as_of=ctx.value_as_of,
                materiality=_scale(abs(loss) / ctx.total_value, 0.05),
            ),
            provenance=PROV_AS_OF_CLOSE,
            amount=-abs(loss),
        )
    ]


@register_producer("long_term_boundary")
def _long_term(ctx: GlanceContext) -> list[Candidate]:
    """Taxable lots reaching a long-term holding period within the horizon (date only —
    no framing about acting before or after it)."""
    summary = ctx.tax_summary()
    grouped: dict[tuple[str, date], list[tax.TaxLot]] = {}
    for lot in summary.lots:
        if lot.term != SHORT_TERM:
            continue
        crosses = lot.open_date + timedelta(days=LONG_TERM_DAYS + 1)
        if 0 <= (crosses - ctx.today).days <= LONG_TERM_HORIZON_DAYS:
            grouped.setdefault((lot.ticker, crosses), []).append(lot)
    out: list[Candidate] = []
    for (ticker, crosses), lots in sorted(grouped.items()):
        qty = sum(lot.quantity for lot in lots)
        gain = sum(lot.unrealized_gain or 0.0 for lot in lots)
        days = (crosses - ctx.today).days
        out.append(
            Candidate(
                ranker.Observation(
                    facet_key="long_term_boundary",
                    text=f"{qty:g} shares of {ticker} reach a long-term holding period on {crosses.isoformat()}.",
                    as_of=ctx.today.isoformat(),
                    materiality=_scale(abs(gain) / ctx.total_value, 0.02) if ctx.total_value else 0.0,
                    urgency=1.0 - days / LONG_TERM_HORIZON_DAYS,
                ),
                provenance=PROV_AS_OF_SYNC,
                ticker=ticker,
                amount=gain,
                event_date=crosses,
            )
        )
    return out


@register_producer("upcoming_earnings")
def _earnings(ctx: GlanceContext) -> list[Candidate]:
    """Cached earnings dates for held tickers (feed facet — never reaches a no-feed build)."""
    cal = calendar.upcoming_events(
        ctx.session,
        ctx.portfolio.tenant_id,
        ctx.portfolio.id,
        today=ctx.today,
        horizon_days=EARNINGS_HORIZON_DAYS,
        macro_events_source=lambda: [],  # portfolio-independent macro prints are not "ahead" for holdings
    )
    as_of = (cal.earnings_sourced_at.date() if cal.earnings_sourced_at else ctx.today).isoformat()
    out: list[Candidate] = []
    for ev in cal.events:
        if ev.kind != "earnings":
            continue
        days = (ev.event_date - ctx.today).days
        out.append(
            Candidate(
                ranker.Observation(
                    facet_key="upcoming_earnings",
                    text=f"{ev.ticker} is scheduled to report earnings on {ev.event_date.isoformat()}.",
                    as_of=as_of,
                    materiality=_scale(ctx.weight_of(ev.ticker), 0.10),
                    urgency=max(0.0, 1.0 - days / EARNINGS_HORIZON_DAYS),
                ),
                provenance=PROV_AS_OF_CLOSE,
                ticker=ev.ticker,
                event_date=ev.event_date,
            )
        )
    return out


# ── Fixed zones ───────────────────────────────────────────────────────────────


def _headline(ctx: GlanceContext, n_brokers: int) -> Headline:
    s = ctx.summary
    last_sync = max((h.broker_as_of for h in ctx.held if h.broker_as_of), default=None)
    day_change = day_pct = None
    day_as_of = day_prov = day_note = None
    tv = ctx.today_view
    if tv is not None and tv.available and not tv.stale and tv.day_gain is not None and tv.as_of_utc:
        day_change, day_pct, day_as_of, day_prov = tv.day_gain, tv.day_pct, tv.as_of_utc, PROV_LIVE
    elif ctx.today_tile is not None and ctx.today_tile.gain is not None and ctx.today_tile.end_date:
        t = ctx.today_tile
        day_change, day_pct, day_as_of, day_prov = t.gain, t.twr, t.end_date.isoformat(), PROV_SETTLED
    else:
        note = ctx.today_tile.note if ctx.today_tile is not None else None
        day_note = note or "Not enough recorded history to form the day's change yet."
    return Headline(
        base_currency=s.base_currency,
        total_value=ctx.total_value,
        market_value=s.market_value,
        cash=s.cash,
        value_as_of=ctx.value_as_of,
        value_provenance=PROV_AS_OF_SYNC if last_sync else PROV_AS_OF_CLOSE,
        last_sync=last_sync.isoformat() if last_sync else None,
        day_change=day_change,
        day_pct=day_pct,
        day_as_of=day_as_of,
        day_provenance=day_prov,
        day_note=day_note,
        n_accounts=s.n_accounts,
        n_brokers=n_brokers,
    )


PATH_LOOKBACK_DAYS = 400  # covers 1Y plus the YTD window in January


def _path(ctx: GlanceContext) -> PathZone:
    rows = ctx.session.execute(
        select(models.NavSnapshot.snap_date, models.NavSnapshot.nav)
        .where(
            models.NavSnapshot.tenant_id == ctx.portfolio.tenant_id,
            models.NavSnapshot.portfolio_id == ctx.portfolio.id,
            models.NavSnapshot.snap_date >= ctx.today - timedelta(days=PATH_LOOKBACK_DAYS),
        )
        .order_by(models.NavSnapshot.snap_date)
    ).all()
    points = [PathPoint(date=d.isoformat(), nav=float(n)) for d, n in rows]
    if len(points) < 2:
        return PathZone(available=False, reason="Not enough recorded history to draw a path yet.", points=points,
                        as_of=points[-1].date if points else None)
    return PathZone(available=True, reason=None, points=points, as_of=points[-1].date)


def _fmt_et(ts: datetime) -> str:
    if ts.tzinfo is None:  # SQLite hands back naive UTC datetimes
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(_ET).strftime("%b %d, %H:%M ET")


def _integrity(ctx: GlanceContext, accounts: list[models.Account]) -> IntegrityZone:
    last_sync = max((h.broker_as_of for h in ctx.held if h.broker_as_of), default=None)
    positions_as_of = last_sync.isoformat() if last_sync else None
    brokers = sorted({a.broker for a in accounts})
    as_of = ctx.now.isoformat()
    base = {"positions_as_of": positions_as_of, "brokers": brokers, "as_of": as_of}
    if not accounts:
        return IntegrityZone(status="no_accounts", healthy=False, open_breaks=0, last_reconciled_at=None,
                             text="No accounts connected yet, so there is nothing to reconcile.", **base)
    if not any(a.broker in _BROKER_FEEDS for a in accounts):
        return IntegrityZone(status="ledger_only", healthy=True, open_breaks=0, last_reconciled_at=None,
                             text="Positions come from imported transactions; there is no custodian feed to reconcile against.",
                             **base)
    open_breaks = ctx.session.scalar(
        select(func.count(models.ReconciliationBreak.id)).where(
            models.ReconciliationBreak.account_id.in_([a.id for a in accounts]),
            models.ReconciliationBreak.resolved_at.is_(None),
        )
    ) or 0
    fetches = ctx.session.scalars(
        select(models.ReconciliationFetchStatus).where(models.ReconciliationFetchStatus.portfolio_id == ctx.portfolio.id)
    ).all()
    successes = [f.last_success_at for f in fetches if f.last_success_at is not None]
    last_ok = max(successes) if successes else None
    last_iso = (last_ok if last_ok.tzinfo else last_ok.replace(tzinfo=UTC)).isoformat() if last_ok else None
    if open_breaks:
        noun = "break" if open_breaks == 1 else "breaks"
        return IntegrityZone(status="breaks", healthy=False, open_breaks=open_breaks, last_reconciled_at=last_iso,
                             text=f"{open_breaks} open reconciliation {noun} against your custodian.", **base)
    if not fetches or last_ok is None:
        return IntegrityZone(status="never", healthy=False, open_breaks=0, last_reconciled_at=None,
                             text="Not yet reconciled against your custodian.", **base)
    if any(reconciliation._is_stale(f, now=ctx.now) for f in fetches):
        return IntegrityZone(status="stale", healthy=False, open_breaks=0, last_reconciled_at=last_iso,
                             text=f"Last reconciled {_fmt_et(last_ok)}; a reconciliation is overdue.", **base)
    return IntegrityZone(status="reconciled", healthy=True, open_breaks=0, last_reconciled_at=last_iso,
                         text=f"Reconciled as of {_fmt_et(last_ok)}.", **base)


# ── Ranked zones ──────────────────────────────────────────────────────────────


def _rank_zones(ctx: GlanceContext) -> tuple[dict[str, RankedZone], Coverage, list[str]]:
    candidates = registry.candidate_facets(ctx.tier, feed_enabled=ctx.feed_enabled)
    admitted = {f.key for f in candidates}
    by_zone: dict[str, list[Candidate]] = {z: [] for z in ZONE_LIMITS}
    unavailable: dict[str, list[str]] = {z: [] for z in ZONE_LIMITS}
    degraded: list[str] = []
    produced = 0
    for key in sorted(PRODUCERS):
        facet = registry.FACET_BY_KEY.get(key)
        if facet is None:  # a facet removed from the registry after its producer registered
            continue
        zone = zone_for(facet)
        if key not in admitted:
            # Excluded by tier, feed, level or enabled-flag: named, never computed.
            if facet.enabled and facet.level == registry.LEVEL_L1:
                unavailable[zone].append(facet.label)
            continue
        try:
            rows = PRODUCERS[key](ctx)
        except Exception:
            # Failure swallowed: one producer raising (a) would otherwise 500 the whole
            # screen, blanking the integrity zone that exists to be never silent. The
            # other zones survive; recorded (c) at ERROR with the traceback and in the
            # payload's ``degraded`` list, which the client renders.
            log.exception("glance producer failed facet=%s portfolio=%s", key, ctx.portfolio.id)
            degraded.append(key)
            continue
        produced += 1
        by_zone[zone].extend(rows)

    zones: dict[str, RankedZone] = {}
    n_obs = 0
    for zone, rows in by_zone.items():
        n_obs += len(rows)
        meta = {id(c.observation): c for c in rows}
        selection = ranker.select([c.observation for c in rows], limit=ZONE_LIMITS[zone], as_of=ctx.value_as_of)
        items: list[GlanceItem] = []
        if not selection.is_floor:
            for obs in selection.observations:
                c = meta[id(obs)]
                facet = registry.FACET_BY_KEY[obs.facet_key]
                items.append(
                    GlanceItem(
                        facet_key=obs.facet_key,
                        family=facet.family,
                        label=facet.label,
                        text=obs.text,
                        as_of=obs.as_of,
                        provenance=c.provenance,
                        surface=facet.surface,
                        score=round(ranker.score(obs), 6),
                        ticker=c.ticker,
                        amount=c.amount,
                        pct=c.pct,
                        event_date=c.event_date.isoformat() if c.event_date else None,
                    )
                )
        floor_as_of = selection.observations[0].as_of if selection.is_floor else ctx.value_as_of
        zones[zone] = RankedZone(
            key=zone,
            items=items,
            is_floor=selection.is_floor,
            floor_text=FLOOR_TEXT_BY_ZONE[zone] if selection.is_floor else None,
            as_of=floor_as_of,
            unavailable=unavailable[zone],
        )
    return zones, Coverage(candidate_facets=len(candidates), produced_facets=produced, observations=n_obs), degraded


# ── Composition ───────────────────────────────────────────────────────────────


def compose(
    session: Session,
    portfolio: models.Portfolio,
    *,
    tier: str,
    feed_enabled: bool,
    now: datetime | None = None,
    quotes_reader: Callable[[], dict | None] | None = None,
) -> GlanceScreen:
    """The whole glance payload for ``portfolio``, whole-portfolio scope."""
    now = now or datetime.now(UTC)
    today = now.astimezone(_ET).date()
    timings: dict[str, float] = {}

    def _timed(name: str, fn: Callable):
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            timings[name] = round((time.perf_counter() - t0) * 1000.0, 2)

    tenant_id, pid = portfolio.tenant_id, portfolio.id
    summary = _timed("summary", lambda: analytics.summary(session, tenant_id, pid))
    held = _timed("holdings", lambda: analytics.valued_holdings(session, tenant_id, pid))
    tiles = _timed(
        "period_tiles",
        lambda: performance.period_tiles(session, tenant_id, pid, today=today, with_benchmarks=False, now=now),
    )
    today_tile = next((t for t in tiles.tiles if t.period == "today"), None)
    today_view = _timed(
        "today",
        lambda: intraday.today_view(session, tenant_id, pid, feed_entitled=feed_enabled, reader=quotes_reader, now=now),
    )
    accounts = list(session.scalars(select(models.Account).where(models.Account.portfolio_id == pid)).all())

    total_value = None
    if summary.market_value is not None or summary.cash is not None:
        total_value = (summary.market_value or 0.0) + (summary.cash or 0.0)
    close_as_of = max((h.last_price_date for h in held if h.last_price_date), default=None)
    value_as_of = (close_as_of or today).isoformat()

    ctx = GlanceContext(
        session=session,
        portfolio=portfolio,
        now=now,
        today=today,
        tier=tier,
        feed_enabled=feed_enabled,
        held=held,
        summary=summary,
        total_value=total_value,
        value_as_of=value_as_of,
        today_tile=today_tile,
        today_view=today_view,
        quotes_reader=quotes_reader,
    )
    headline = _headline(ctx, n_brokers=len({a.broker for a in accounts}))
    path = _timed("path", lambda: _path(ctx))
    zones, coverage, degraded = _timed("ranked", lambda: _rank_zones(ctx))
    integrity = _timed("integrity", lambda: _integrity(ctx, accounts))

    return GlanceScreen(
        portfolio_id=str(pid),
        generated_at=now.isoformat(),
        state=render_state(now),
        tier=tier,
        feed_enabled=feed_enabled,
        headline=headline,
        path=path,
        movers=zones[ZONE_MOVERS],
        insights=zones[ZONE_INSIGHTS],
        ahead=zones[ZONE_AHEAD],
        integrity=integrity,
        coverage=coverage,
        degraded=degraded,
        timings_ms=timings,
    )
