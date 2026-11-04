"""The Holdings market note: why the portfolio moved differently from the market.

Brian, 2026-11-04: under the index + portfolio strip, a short daily blurb on the
variance between the portfolio and the markets ("today my portfolio is down but
the markets are up, I think it has something to do with energy"). It is made
only when the page asks for it, so a day nobody opens the dashboard costs
nothing, and the page loads it after everything else, so it never delays the
rest of Holdings.

**Deterministic, from figures already on screen.** Three facts, each from a
module that already reconciles to the number the reader sees:

1. the TODAY tile (``performance.period_tiles``): the portfolio's return and
   each index's return over the same settled session;
2. sector attribution (``attribution.compute_attribution``) over that same
   session vs the S&P 500: which sector explains the most of the gap, and
   whether it was the weighting (allocation) or the names held (selection);
3. name-level drivers (``benchmark_gap.compute_benchmark_gap``): the holdings
   and index members that moved the gap most.

A leg that cannot be computed is left out of the note, never guessed; the
note then says what it is missing. No generated prose: every sentence is a
template over those numbers, so the note is cheap, repeatable and cannot say
anything the data does not.

Cached per portfolio, account selection and session (``compute_cache``, keyed
by the portfolio's content fingerprint), so reopening the page the same day
reuses it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

from api.services import attribution as attribution_service
from api.services import benchmark_gap as gap_service
from api.services import compute_cache

#: The index the note compares against first (the attribution's benchmark).
LEAD_SYMBOL = "SPY"
LEAD_LABEL = "S&P 500"
#: Below this (in return-fraction units, 0.0005 = 0.05 pt) a gap is "in line".
IN_LINE = 0.0005
#: How many names to name on each side.
NAMES_PER_SIDE = 2
CACHE_VERSION = "v1"


@dataclass
class MarketNote:
    available: bool
    session_date: date | None = None
    sentences: list[str] = field(default_factory=list)
    #: Legs that could not be computed, each with its reason (shown small).
    missing: list[str] = field(default_factory=list)
    portfolio_return: float | None = None
    lead_return: float | None = None
    gap: float | None = None


def _pts(x: float) -> str:
    return f"{abs(x) * 100:.1f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}{abs(x) * 100:.1f}%"


def _moved(x: float) -> str:
    if abs(x) < IN_LINE:
        return "was flat"
    return f"{'rose' if x > 0 else 'fell'} {_pts(x)}%"


def _day(d: date) -> str:
    return f"{d:%A} {d:%b} {d.day}"


def _headline(tile) -> tuple[str | None, float | None, float | None]:
    lead = next((b for b in tile.benchmarks if b.symbol == LEAD_SYMBOL and b.ret is not None), None)
    if tile.twr is None or tile.end_date is None:
        return None, None, None
    when = _day(tile.end_date)
    if lead is None:
        return f"On {when} your portfolio {_moved(tile.twr)}.", tile.twr, None
    gap = tile.twr - lead.ret
    if abs(gap) < IN_LINE:
        relation = f"in line with the {LEAD_LABEL} ({_signed(lead.ret)})"
    else:
        relation = (f"while the {LEAD_LABEL} {_moved(lead.ret)}, "
                    f"{'ahead' if gap > 0 else 'behind'} by {_pts(gap)} points")
    others = [f"{b.label} {_signed(b.ret)}" for b in tile.benchmarks
              if b.symbol != LEAD_SYMBOL and b.ret is not None]
    tail = f" ({', '.join(others)})" if others else ""
    return f"On {when} your portfolio {_moved(tile.twr)} {relation}{tail}.", tile.twr, lead.ret


def _sector_sentence(attr) -> str | None:
    """The sector that explains the most of the gap, and how."""
    if attr is None or not attr.computable or not attr.sectors or attr.active_return is None:
        return None
    gap = attr.active_return
    if abs(gap) < IN_LINE:
        return None
    # The sector whose effect pushed hardest in the direction of the gap.
    same_way = [s for s in attr.sectors if (s.total > 0) == (gap > 0) and abs(s.total) >= IN_LINE / 2]
    if not same_way:
        return None
    s = max(same_way, key=lambda e: abs(e.total))
    verb = "helped" if s.total > 0 else "hurt"
    share = min(abs(s.total) / abs(gap), 1.0)
    lead = "Most of the gap" if share >= 0.5 else "The largest part of the gap"
    pw, bw = s.port_weight * 100, s.bench_weight * 100
    if abs(s.allocation) >= abs(s.selection) and s.bench_return is not None:
        side = "overweight" if s.port_weight > s.bench_weight else "underweight"
        return (f"{lead} came from {s.sector}: you are {side} ({pw:.0f}% of the portfolio "
                f"vs {bw:.0f}% of the {LEAD_LABEL}) and the sector {_moved(s.bench_return)}, "
                f"which {verb} by {_pts(s.total)} points.")
    if s.port_return is not None and s.bench_return is not None:
        return (f"{lead} came from your {s.sector} holdings, which {_moved(s.port_return)} "
                f"against {_signed(s.bench_return)} for the sector; that {verb} by "
                f"{_pts(s.total)} points.")
    return None


def _name(d) -> str:
    held = {"not_held": "not held", "underweight": "underweight",
            "overweight": "overweight", "held": "held"}.get(d.classification, d.classification)
    tag = ", earnings" if d.earnings else ""
    return f"{d.symbol} ({_signed(d.ret)}, {held}{tag})"


def _names_sentence(gap) -> str | None:
    if gap is None or not gap.computable or not gap.drivers:
        return None
    drags = sorted((d for d in gap.drivers if d.active_contribution < 0),
                   key=lambda d: d.active_contribution)[:NAMES_PER_SIDE]
    helps = sorted((d for d in gap.drivers if d.active_contribution > 0),
                   key=lambda d: -d.active_contribution)[:NAMES_PER_SIDE]
    parts = []
    if drags:
        parts.append("Biggest drags: " + ", ".join(_name(d) for d in drags))
    if helps:
        parts.append("biggest help: " + ", ".join(_name(d) for d in helps))
    if not parts:
        return None
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def compose(tile, attr=None, gap=None) -> MarketNote:
    """The note from already-computed legs. Pure: no I/O, so it is tested directly."""
    if tile is None or tile.twr is None or tile.end_date is None:
        reason = (tile.note if tile is not None and tile.note else
                  "today's session is not in the portfolio's history yet")
        return MarketNote(available=False, missing=[reason])
    headline, port, lead = _headline(tile)
    sentences = [headline] if headline else []
    missing: list[str] = []
    sector = _sector_sentence(attr)
    if sector:
        sentences.append(sector)
    elif attr is not None and not attr.computable and attr.reason:
        missing.append(f"sector view: {attr.reason}")
    names = _names_sentence(gap)
    if names:
        sentences.append(names)
    elif gap is not None and not gap.computable and gap.reason:
        missing.append(f"names: {gap.reason}")
    return MarketNote(
        available=True, session_date=tile.end_date, sentences=sentences, missing=missing,
        portfolio_return=port, lead_return=lead,
        gap=(port - lead) if port is not None and lead is not None else None,
    )


def market_note(
    session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    tile,
    account_ids: Collection[uuid.UUID] | None = None,
    attr_enabled: bool = True,
    gap_enabled: bool = True,
) -> MarketNote:
    """Compute (or reuse today's) note for one portfolio. ``tile`` is the TODAY
    ``PeriodTile`` the caller built exactly as the Holdings strip does."""
    if tile is None or tile.end_date is None or tile.start_date is None or tile.twr is None:
        return compose(tile)
    accounts = ",".join(sorted(str(a) for a in account_ids)) if account_ids else "all"
    fp = compute_cache.portfolio_fingerprint(session, tenant_id, portfolio_id)
    key = (f"market-note:{CACHE_VERSION}:{portfolio_id}:{accounts}:{tile.end_date}:"
           f"{int(attr_enabled)}{int(gap_enabled)}:{fp}")

    def build() -> MarketNote:
        attr = None
        if attr_enabled:
            lookback = max(1, (tile.end_date - tile.start_date).days)
            attr = attribution_service.compute_attribution(
                session, tenant_id, portfolio_id, today=tile.end_date, lookback_days=lookback,
                do_backfill=False, account_ids=account_ids, benchmark=LEAD_SYMBOL,
            )
            # A window other than the tile's would explain a different gap.
            if attr.computable and (attr.start_date != tile.start_date or attr.as_of != tile.end_date):
                attr = attribution_service.AttributionSummary(
                    computable=False, benchmark=LEAD_SYMBOL,
                    reason="sector prices do not cover the same session yet")
        gap = None
        if gap_enabled and not account_ids:
            gap = gap_service.compute_benchmark_gap(
                session, tenant_id, portfolio_id, index="SPX", tile=tile)
        return compose(tile, attr, gap)

    return compute_cache.cached(key, build)
