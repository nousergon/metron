"""Backfill a gap in the daily NAV snapshot series from each missed day's own closes.

``daily-refresh`` records "today" only (``record_snapshot`` / ``record_account_snapshots``),
so a run of failed fires leaves a hole in both ``nav_snapshots`` and
``account_nav_snapshots``. ``reconstruct_snapshots`` does not fill it: it writes month-ends,
flow dates and endpoints, not every day. This module fills such a hole, and only where it
can do so honestly.

How a missed day is valued. For each series (one portfolio, or one account) the gap is
bracketed by the last snapshot before it (A) and the first after it (B). Both carry their
priced ``composition``. If A and B hold the same quantity of every leg, the positions did
not change inside the gap, so each missed day ``d`` is valued as

    Σ qty × close(ticker, as of d) × fx(ccy, as of d) × m

where ``m`` is the leg's recorded ``value / (qty × price × fx)`` (1 for a share, 1/100 for a
bond quoted per 100 face). Nothing is copied forward: every leg is priced from ``d``'s own
close. A series is SKIPPED, with the reason reported, when:

- it has no snapshot on one side of the gap, or no composition to value from;
- any leg's quantity differs between A and B (a trade inside the gap, timing unknown);
- a leg has no close history and its price differs between A and B (a broker-priced
  holding whose daily price is unknown) — a constant-priced leg such as cash is kept;
- a priced leg has no close for ``d`` on a US session (SPY closed that day), or its close
  history stops before ``d``;
- the method does not reproduce B's own recorded NAV within ``_SELF_CHECK_TOLERANCE``.

What it writes. One row per (series, missed day), insert-only: a day that already has a
row is never touched. ``external_flow`` is 0 (positions were unchanged); B keeps its own
flow, which already spans the whole gap, so TWR counts each flow once. Every row is final
(``provisional`` False) and carries ``composition["backfill"]``, which ``undo`` matches on,
so undo deletes exactly what this wrote and nothing a live refresh wrote.

Opt-in methods (metron-ops-I361). Each is off unless its :class:`Options` field is set, and
with every field at its default the behaviour above is unchanged, byte for byte:

- ``restate_anchors`` — a bracketing A or B row still ``provisional`` (a fund leg recorded
  before its NAV struck, and never restated because the reconcile window passed) is
  restated with ``performance._restate_provisional`` BEFORE it is used as an anchor. The
  prior NAV, flag and legs are stashed in ``composition["backfill_restate"]``.
- ``trade_aware`` — when holdings differ across the gap, positions on ``d`` are
  ``q_A + Σ`` signed quantities traded in ``(A, d]``, from the transaction ledger or, failing
  that, the broker lots — whichever reconciles A to B EXACTLY for every leg. Inserted rows
  carry the live writer's own flow (net purchases since the previous row) and B's flow is
  rebased to ``(last inserted day, B]``, the original stashed in
  ``composition["backfill_rebase"]``. Flows must be conserved to the cent.
- ``missing_close="carry"`` — on a missed day only, a leg with no close for that session
  carries its previous close, flagged ``stale`` with its real ``price_date``; the carried
  legs may not exceed ``_CARRY_CAP`` of that day's NAV.
- ``proxy_bridge`` — a broker-priced leg (no close history, price moved A→B) is bridged by
  a named listed proxy's returns from A's recorded price, the residual against B's recorded
  price spread geometrically over the sessions so both ends match exactly; capped at
  ``_BRIDGE_CAP`` of NAV.
- ``portfolio_from_accounts`` — a portfolio-grain missed day is the sum of its accounts'
  rows (``nav``, ``external_flow``, ``cost_basis``, legs concatenated), only when EVERY
  account of the cohort has a row that day and the accounts sum to the portfolio at A and
  B within ``_COHORT_IDENTITY_TOLERANCE``. An end whose ``nav`` was rewritten by
  ``reconstruct_snapshots`` (it no longer equals the sum of its own recorded legs) is
  compared on its legs — the live writer's basis — and the residual is reported in
  ``notes``. A failed identity names both ends' sums, diff and % in the skip reason.
  A leg whose quantity changed with NO trade in the chosen source is reconciled only by
  one of two corporate events, each reported in ``notes``:

  * ``split`` — ``b_qty / a_qty`` is a simple ratio and the leg's close series proves it.
    Either the series is UNADJUSTED across the split: exactly one session inside ``(A, B]``
    whose close over the previous close is ``1/ratio`` within ``_SPLIT_PRICE_TOLERANCE``
    (that day is the split date: ``a_qty`` before it, ``b_qty`` from it, no flow); or the
    series is SPLIT-ADJUSTED (the price cache's history comes from a source that
    back-adjusts every bar before a corporate action, so a refresh after the split rewrites
    A's close): A's stored close times the ratio matches A's recorded price, B's stored
    close matches B's, and no day jumps. On an adjusted series every day is valued at
    ``b_qty`` × the adjusted close — ``a_qty × raw = b_qty × raw / ratio`` — which needs no
    split date. A recorded ``SPLIT`` transaction in the ledger names the date and must
    agree on the ratio; it never stands in for the price evidence. No matching evidence,
    no split: the series is skipped with the reason.
  * ``drip`` — a small increase (≤ ``_DRIP_MAX_FRACTION`` of the position) with no trade:
    a dividend reinvested into shares. The dividend is return, not an external flow, so no
    flow is recorded. The quantity steps on the latest ``DIVIDEND`` the ledger records for
    the leg inside the gap, or, with none recorded, on the first day after A.
- ``anchor="proportional"`` — a series whose reprice misses a recorded end by more than
  ``_SELF_CHECK_TOLERANCE`` (but by at most ``_ANCHOR_MAX_ERROR`` at both ends) is scaled
  by ``f(d) = f_A + (f_B − f_A) × (d − A) / (B − A)``, ``f`` = recorded NAV / repriced NAV,
  so both ends match exactly; every scaled day is flagged estimated (``how`` "anchor").
- ``leg_prices`` — operator-supplied ``ticker -> {date: price}`` used as that leg's closes
  (a broker-priced fund with no close history). A missing price for any US session inside
  the gap is an error (:class:`LegPricesError`), never a skip. Days so priced are flagged
  estimated (``how`` "operator-price").
- ``estimate_broker_legs`` — a broker-priced leg (no close history, a recorded price at both
  A and B) is estimated autonomously: each US session's price is log-linear in trading-day
  steps between the known prices around it — A's and B's recorded prices, and any day the
  operator supplied through ``leg_prices``, which is never overridden — so
  ``p(d) = p_lo × (p_hi / p_lo) ^ (n(d) − n(lo)) / (n(hi) − n(lo))``, ``n`` counting sessions
  after A. A non-session day carries the last session's estimate. Each such day is flagged
  estimated (``how`` "interpolated"). REFUSED, with the reason, when the leg weighs more than
  ``_INTERP_MAX_WEIGHT`` of the series NAV (at A or at B) AND moved more than
  ``_INTERP_MAX_MOVE`` from A to B: a large, volatile leg is not straight-lined. A leg named
  in ``proxy_bridge`` is bridged, not interpolated.
- ``only`` — restrict the run to the named account ids, portfolio ids, or ``"portfolio"``.
- ``explain`` — for every skipped series, list every failing leg with a cause code; for a
  series that fails the self-check, every leg's recorded vs repriced value at A and B; for
  a skipped portfolio, the sum of its accounts' NAVs vs the portfolio row at A and B; and,
  for any series holding a broker-priced leg, which of them ``estimate_broker_legs``
  interpolates or refuses (``broker_legs``), whether or not that option is set.

Rows written under any option carry ``method``, ``estimated`` and ``estimated_legs`` in
``composition["backfill"]``; ``undo`` also restores every stashed flow and anchor.
"""

from __future__ import annotations

import bisect
import copy
import logging
import math
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from operator import itemgetter

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from api.db import models
from api.services import fx as fx_service
from api.services import performance
from api.services import prices as price_service
from portfolio_analytics.domain.ledger import TxnType

logger = logging.getLogger(__name__)

BACKFILL_MARKER = "backfill-snapshot-gap"
# B's recorded NAV must be reproduced this closely by the gap pricing method, or the
# series is not trusted (fund legs and FX make an exact match unusual; 0.5% is far below
# any plausible one-day move the backfill could be mistaken for).
_SELF_CHECK_TOLERANCE = 0.005
_QTY_REL_TOLERANCE = 1e-9
_HISTORY_LOOKBACK_DAYS = 14
# Opt-in method bounds (metron-ops-I361).
_CARRY_CAP = 0.05                  # carried legs, as a share of that day's NAV
_BRIDGE_CAP = 0.10                 # a proxy-bridged leg, as a share of NAV
_COHORT_IDENTITY_TOLERANCE = 0.001  # Σ accounts vs the portfolio row, at A and at B
_TRADE_DAY_RETURN_CAP = 0.10       # |(nav − flow) / nav_prev − 1| on a trade day
_CENT = 0.005
_ANCHOR_MAX_ERROR = 0.02           # proportional anchoring refuses an end missed by more
_SPLIT_PRICE_TOLERANCE = 0.02      # close ratio vs 1/split ratio (and recorded vs stored close)
_SPLIT_MAX_TERM = 100              # a "simple" ratio: both terms at most this,
_SPLIT_MAX_SMALL_TERM = 10         # and the smaller at most this (5:1, 3:2, 1:20 — not 73:100)
_SPLIT_MIN_MOVE = math.log(1.2)    # |ln ratio| below this is indistinguishable from a day's move
_DRIP_MAX_FRACTION = 0.02          # a reinvested dividend adds at most this share of the position
_INTERP_MAX_WEIGHT = 0.25          # an interpolated leg above this share of NAV ...
_INTERP_MAX_MOVE = 0.05            # ... that moved more than this A -> B is refused

# Why a leg cannot be priced on a day (``--explain`` cause codes).
CAUSES = {
    1: "recorded price or FX rate is empty",
    2: "no close history and the price differs between A and B (broker-priced)",
    3: "close history stops before this day",
    4: "no close on this US session",
    5: "no FX rate as of this day",
}


@dataclass
class Options:
    """Opt-in behaviour. ``Options()`` is exactly the original tool."""

    explain: bool = False
    restate_anchors: bool = False
    trade_aware: bool = False
    missing_close: str = "skip"  # "skip" | "carry"
    proxy_bridge: dict[str, str] = field(default_factory=dict)  # broker-priced ticker -> listed proxy
    portfolio_from_accounts: bool = False
    only: frozenset[str] = frozenset()  # account id | portfolio id | "portfolio"
    anchor: str = "none"  # "none" | "proportional"
    leg_prices: dict[str, dict[date, float]] = field(default_factory=dict)  # ticker -> date -> price
    estimate_broker_legs: bool = False

    @property
    def extended(self) -> bool:
        return self != Options()

    def selects(self, plan: SeriesPlan) -> bool:
        if not self.only:
            return True
        wanted = {t.strip().lower() for t in self.only}
        if plan.grain == "portfolio":
            return "portfolio" in wanted or str(plan.portfolio_id) in wanted
        return str(plan.account_id) in wanted


class LegPricesError(ValueError):
    """``leg_prices`` cannot price a day it was asked to: never a quiet skip."""


@dataclass
class SeriesPlan:
    grain: str  # "portfolio" | "account"
    tenant_id: uuid.UUID
    portfolio_id: uuid.UUID
    account_id: uuid.UUID | None
    missing: list[date]
    points: dict[date, float] = field(default_factory=dict)
    skipped: str | None = None
    self_check: float | None = None  # worst relative error re-pricing A and B
    cost_basis: float = 0.0
    legs: dict[date, list[dict]] = field(default_factory=dict)
    spy: dict[date, float | None] = field(default_factory=dict)
    # Opt-in methods (metron-ops-I361); all empty on a default run.
    method: str = "reprice"
    flows: dict[date, float] = field(default_factory=dict)
    cost_bases: dict[date, float] = field(default_factory=dict)
    estimated_legs: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    explain: dict | None = None
    # (row, prior state, restated state) — the states are plain values, so a dry run's
    # rollback (which expires the row) cannot change what is reported.
    restated: list[tuple[object, dict, dict]] = field(default_factory=list)
    rebase: tuple[object, float, float] | None = None  # (B row, original flow, rebased flow)
    existing_in_gap: dict[date, float] = field(default_factory=dict)  # date -> recorded nav

    @property
    def estimated(self) -> bool:
        return bool(self.estimated_legs)

    def updates(self) -> list[dict]:
        out = [{
            "kind": "restate-anchor", "date": after["date"],
            "nav_before": round(prior["nav"], 2), "nav_after": round(after["nav"], 2),
            "still_provisional": after["provisional"],
        } for _row, prior, after in self.restated]
        if self.rebase is not None and not self.skipped:
            row, before, after = self.rebase
            out.append({"kind": "rebase-flow", "date": row.snap_date.isoformat(),
                        "external_flow_before": round(before, 2), "external_flow_after": round(after, 2)})
        return out

    def as_dict(self, extended: bool = False) -> dict:
        out = {
            "grain": self.grain,
            "tenant_id": str(self.tenant_id),
            "portfolio_id": str(self.portfolio_id),
            "account_id": str(self.account_id) if self.account_id else None,
            "missing": [d.isoformat() for d in self.missing],
            "points": {d.isoformat(): round(v, 2) for d, v in sorted(self.points.items())},
            "skipped": self.skipped,
            "self_check": None if self.self_check is None else round(self.self_check, 6),
        }
        if extended:
            out["method"] = self.method
            out["estimated"] = self.estimated
            out["estimated_legs"] = self.estimated_legs
            out["flows"] = {d.isoformat(): round(v, 2) for d, v in sorted(self.flows.items()) if v}
            out["updates"] = self.updates()
            out["notes"] = self.notes
            if self.explain is not None:
                out["explain"] = self.explain
        return out


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _leg_key(leg: dict) -> tuple[str, str]:
    return (leg["ticker"], (leg.get("currency") or "USD").upper())


def _aggregate(legs: list[dict]) -> dict[tuple[str, str], dict]:
    """Sum quantity and value per (ticker, currency). A portfolio composition lists one leg
    per account holding, so the same ticker can appear more than once."""
    out: dict[tuple[str, str], dict] = {}
    for leg in legs:
        k = _leg_key(leg)
        agg = out.setdefault(k, {"qty": 0.0, "value": 0.0, "price": leg.get("price"),
                                 "fx_rate": leg.get("fx_rate"), "is_fund": bool(leg.get("is_fund"))})
        agg["qty"] += float(leg.get("qty") or 0.0)
        agg["value"] += float(leg.get("value") or 0.0)
    return out


def _same_qty(a: float, b: float) -> bool:
    return abs(a - b) <= _QTY_REL_TOLERANCE * max(1.0, abs(a), abs(b))


def _asof(series, when: date):
    if not series:
        return None
    i = bisect.bisect_right([p.bar_date for p in series], when)
    return series[i - 1] if i else None


class _Valuer:
    """Prices a series' legs on any day from A/B's recorded compositions.

    ``refs`` is the recorded leg each position is valued from: B's, on the default path
    (exactly the original tool); B's when B holds it and A's otherwise, on the trade-aware
    path (a leg sold out inside the gap is still priced on the days it was held)."""

    def __init__(self, a, b, a_agg, b_agg, refs, history, fx_hist, opts: Options):
        self.a, self.b, self.a_agg, self.b_agg, self.refs = a, b, a_agg, b_agg, refs
        self.history, self.fx_hist, self.opts = history, fx_hist, opts
        # A US session is a day SPY closed on. On one, every USD leg with a close history
        # must have its own close for that day; carrying an earlier one is the copy-forward
        # this module exists to avoid (unless ``missing_close="carry"`` opts in, per leg,
        # flagged). Non-session days (weekends, holidays) carry as-of, which is what a live
        # weekend snapshot does too.
        self.us_sessions = {p.bar_date for p in history.get("SPY") or []}
        # Broker-priced legs estimated by interpolation (``estimate_broker_legs``); set by the
        # planner once the refusal rule has passed. Empty on every other path.
        self.interp: frozenset = frozenset()

    def _bridge(self, ticker: str, ccy: str, d: date):
        """Proxy-bridged price for a gap day, or None when the proxy has no usable close."""
        proxy = self.opts.proxy_bridge.get(ticker)
        series = self.history.get(proxy) if proxy else None
        pa, pb, pd = (_asof(series, x) for x in (self.a.snap_date, self.b.snap_date, d))
        if pa is None or pb is None or pd is None or not pa.close or not pb.close:
            return None
        p_a, p_b = float(self.a_agg[(ticker, ccy)]["price"]), float(self.b_agg[(ticker, ccy)]["price"])
        residual = (p_b / p_a) / (pb.close / pa.close)
        sessions = sorted(s for s in self.us_sessions if self.a.snap_date < s <= self.b.snap_date)
        if sessions:
            frac = sum(1 for s in sessions if s <= d) / len(sessions)
        else:
            frac = (d - self.a.snap_date).days / (self.b.snap_date - self.a.snap_date).days
        return p_a * (pd.close / pa.close) * residual ** frac, proxy

    def _interpolated(self, k, d: date) -> float:
        """Log-linear in trading-day steps between the known prices bracketing ``d``: A's
        and B's recorded prices and any operator-supplied day strictly between them."""
        lo_d, hi_d = self.a.snap_date, self.b.snap_date
        knots = {lo_d: float(self.a_agg[k]["price"]), hi_d: float(self.b_agg[k]["price"])}
        for x, px in (self.opts.leg_prices.get(k[0]) or {}).items():
            if lo_d < x < hi_d:
                knots[x] = float(px)
        order = sorted(knots)
        i = bisect.bisect_right(order, d)
        lo, hi = order[i - 1], order[i]  # A < d < B, so both exist and differ
        n = sorted(s for s in self.us_sessions if lo < s <= hi)
        if n:
            frac = sum(1 for s in n if s <= d) / len(n)
        else:
            frac = (d - lo).days / (hi - lo).days
        return knots[lo] * (knots[hi] / knots[lo]) ** frac

    def _operator_price(self, k, d: date, anchor: str | None):
        """``(px, px_date, how)`` from ``opts.leg_prices``. A recorded end with no supplied
        price keeps its recorded price; a missed US session with none is an error — unless
        the leg is interpolated (``estimate_broker_legs``), which fills the unsupplied days."""
        supplied = self.opts.leg_prices[k[0]]
        if d in supplied:
            return supplied[d], d, None if anchor else "operator-price"
        if anchor is not None:
            ref = (self.a_agg if anchor == "A" else self.b_agg).get(k)
            return 2 if ref is None or not ref["price"] else (ref["price"], None, None)
        if k in self.interp:
            return self._interpolated(k, d), None, "interpolated"
        if d in self.us_sessions:
            raise LegPricesError(f"--leg-prices has no price for {k[0]} on {d.isoformat()} (a US session)")
        earlier = [x for x in supplied if self.a.snap_date <= x < d]
        if earlier:
            x = max(earlier)
            return supplied[x], x, "operator-price"
        ref = self.a_agg.get(k)
        return 2 if ref is None or not ref["price"] else (ref["price"], None, "operator-price")

    def price(self, k, d: date, anchor: str | None):
        """``(px, px_date, rate, how, proxy)`` or an int cause code. ``anchor`` is "A"/"B"
        when ``d`` is a recorded end, None for a missed day."""
        ticker, ccy = k
        how, proxy = None, None
        if ticker in self.opts.leg_prices:
            got = self._operator_price(k, d, anchor)
            if isinstance(got, int):
                return got
            px, px_date, how = got
            rate = self.fx_hist.rate_as_of(ccy, d)
            return 5 if rate is None else (px, px_date, rate, how, None)
        series = self.history.get(ticker)
        carry = anchor is None and self.opts.missing_close == "carry"
        if series:
            if series[-1].bar_date < d:
                return 3  # history stops before d (and so before B): nothing to carry from
            else:
                bar = _asof(series, d)
                if bar is None:
                    return 3
                if ccy == "USD" and d in self.us_sessions and bar.bar_date != d:
                    if not carry:
                        return 4  # a US session with no close for this leg: never carry one
                    how = "carry"
            px, px_date = bar.close, bar.bar_date
        else:
            # No close history (cash, or a broker-priced holding). Only a price that is the
            # same at both ends is known for the days between.
            al, bl = self.a_agg.get(k), self.b_agg.get(k)
            if al is not None and bl is not None and al["price"] == bl["price"]:
                px, px_date = bl["price"], None
            elif k in self.interp:
                px = al["price"] if anchor == "A" else bl["price"] if anchor == "B" else self._interpolated(k, d)
                px_date, how = None, None if anchor else "interpolated"
            elif ticker in self.opts.proxy_bridge and al is not None and bl is not None \
                    and al["price"] and bl["price"]:
                if anchor == "A":
                    px = al["price"]
                elif anchor == "B":
                    px = bl["price"]
                else:
                    got = self._bridge(ticker, ccy, d)
                    if got is None:
                        return 2
                    px = got[0]
                px_date, how, proxy = None, "proxy-bridge", self.opts.proxy_bridge[ticker]
            else:
                return 2
        rate = self.fx_hist.rate_as_of(ccy, d)
        if rate is None:
            return 5
        return px, px_date, rate, how, proxy

    def value_on(self, d: date, qty_of: dict, anchor: str | None = None, causes: list | None = None):
        """``(total, legs, estimated)`` for ``d`` at quantities ``qty_of``; None means "cannot
        price this day honestly". With ``causes`` given, every failing leg is recorded there
        instead of stopping at the first."""
        total, legs, estimated = 0.0, [], []
        failed = False
        for k, ref in self.refs.items():
            ticker, ccy = k
            qty, value = qty_of.get(k, 0.0), ref["value"]
            if qty == 0 or value == 0:
                continue
            price_ref = ref["price"]
            fx_ref = ref["fx_rate"] if ref["fx_rate"] is not None else 1.0
            got = 1 if (not price_ref or not fx_ref) else self.price(k, d, anchor)
            if isinstance(got, int):
                if causes is None:
                    return None
                causes.append({"ticker": ticker, "currency": ccy, "date": d.isoformat(),
                               "cause": got, "cause_text": CAUSES[got]})
                failed = True
                continue
            px, px_date, rate, how, proxy = got
            mult = value / (ref["qty"] * price_ref * fx_ref)
            leg_value = qty * px * rate * mult
            total += leg_value
            legs.append({
                "ticker": ticker, "qty": qty, "price": px,
                "price_date": px_date.isoformat() if px_date else None,
                "currency": ccy, "fx_rate": rate, "value": leg_value,
                "is_fund": ref["is_fund"], "stale": how == "carry", "proxy": proxy,
            })
            if how is not None:
                estimated.append({"ticker": ticker, "date": d.isoformat(), "how": how, "value": leg_value,
                                  "price_date": px_date.isoformat() if px_date else None, "proxy": proxy})
        if failed:
            return None
        return total, legs, estimated


def _broker_legs(a, b, a_agg: dict, b_agg: dict, history: dict, opts: Options, missing: list[date]) -> list[dict]:
    """Every broker-priced leg ``estimate_broker_legs`` would act on, and its verdict.

    A candidate has no close history and a recorded price at both A and B that differs (or
    is a ``leg_prices`` ticker, whose unsupplied days are filled). Verdicts: "interpolate";
    "refuse" (over ``_INTERP_MAX_WEIGHT`` of NAV and moved over ``_INTERP_MAX_MOVE``);
    "leg-prices" (every missed US session supplied: nothing to estimate); "proxy-bridge"."""
    if a is None or b is None:
        return []
    sessions = {p.bar_date for p in history.get("SPY") or []}
    a_nav, b_nav = float(a.nav or 0.0), float(b.nav or 0.0)
    out = []
    for k in sorted(set(a_agg) & set(b_agg)):
        al, bl = a_agg[k], b_agg[k]
        supplied = opts.leg_prices.get(k[0])
        if history.get(k[0]) or not al["price"] or not bl["price"]:
            continue
        if al["price"] == bl["price"] and supplied is None:
            continue  # constant-priced (cash): already valued exactly
        move = float(bl["price"]) / float(al["price"]) - 1
        weights = [x["value"] / nav for x, nav in ((al, a_nav), (bl, b_nav)) if nav]
        weight = max((abs(w) for w in weights), default=None)
        row = {"ticker": k[0], "currency": k[1], "a_price": al["price"], "b_price": bl["price"],
               "move": round(move, 6), "weight": None if weight is None else round(weight, 6)}
        if k[0] in opts.proxy_bridge:
            row["verdict"] = "proxy-bridge"
        elif supplied is not None and all(d in supplied for d in missing if d in sessions):
            row["verdict"] = "leg-prices"
        elif weight is not None and weight > _INTERP_MAX_WEIGHT and abs(move) > _INTERP_MAX_MOVE:
            row["verdict"] = "refuse"
            row["reason"] = (f"{k[0]} is {weight:.1%} of NAV (over {_INTERP_MAX_WEIGHT:.0%}) and moved "
                             f"{move:+.1%} from A to B (over {_INTERP_MAX_MOVE:.0%}): a large, volatile "
                             "broker-priced leg is not straight-lined")
        else:
            row["verdict"] = "interpolate"
        if supplied is not None and row["verdict"] in ("interpolate", "refuse"):
            row["supplied_days"] = sum(1 for d in supplied if a.snap_date < d < b.snap_date)
        out.append(row)
    return out


@dataclass
class _Ctx:
    """What ``_plan_series`` needs beyond the two anchors; session-bound reads are callables."""

    history: dict
    fx_hist: object
    opts: Options = field(default_factory=Options)
    trades: dict[str, list[tuple[date, str, float]]] | None = None  # source -> (date, ticker, ±qty)
    flow_of: Callable[[date, date], float] | None = None  # live net purchases over (after, through]
    # Recorded corporate events in (A, B]: {"splits": {ticker: [(date, ratio)]},
    # "dividends": {ticker: [date]}} — read only under trade_aware.
    corporate: dict | None = None


def _explain(plan: SeriesPlan, a, b, a_agg, b_agg, ctx: _Ctx) -> dict:
    """Every failing leg of a skipped series, each with its cause, weight and A/B facts."""
    out: dict = {
        "a_date": a.snap_date.isoformat() if a is not None else None,
        "b_date": b.snap_date.isoformat() if b is not None else None,
        "a_provisional": None if a is None else bool(a.provisional),
        "b_provisional": None if b is None else bool(b.provisional),
        "legs": [],
    }
    if a is None or b is None or not a_agg or not b_agg:
        return out
    a_nav, b_nav = float(a.nav or 0.0), float(b.nav or 0.0)

    def facts(k) -> dict:
        al, bl = a_agg.get(k) or {}, b_agg.get(k) or {}
        weight = (bl.get("value") or 0.0) / b_nav if bl and b_nav else \
            ((al.get("value") or 0.0) / a_nav if a_nav else None)
        return {"a_qty": al.get("qty"), "b_qty": bl.get("qty"), "a_price": al.get("price"),
                "b_price": bl.get("price"), "weight": None if weight is None else round(weight, 6)}

    for k in sorted(set(a_agg) | set(b_agg)):
        if k not in a_agg or k not in b_agg:
            cause = "only-at-B" if k not in a_agg else "only-at-A"
        elif not _same_qty(a_agg[k]["qty"], b_agg[k]["qty"]):
            cause = "qty-changed"
        else:
            continue
        out["legs"].append({"ticker": k[0], "currency": k[1], "date": None, "cause": cause,
                            "cause_text": "quantity differs between A and B", **facts(k)})
    refs = {k: (b_agg.get(k) if k in b_agg else a_agg[k]) for k in list(b_agg) + [k for k in a_agg if k not in b_agg]}
    valuer = _Valuer(a, b, a_agg, b_agg, refs, ctx.history, ctx.fx_hist, ctx.opts)
    if ctx.opts.estimate_broker_legs:
        valuer.interp = frozenset((x["ticker"], x["currency"]) for x in _broker_legs(
            a, b, a_agg, b_agg, ctx.history, ctx.opts, plan.missing) if x["verdict"] == "interpolate")
    for d, anchor, qty_src in ((a.snap_date, "A", a_agg), (b.snap_date, "B", b_agg),
                               *((d, None, b_agg) for d in plan.missing)):
        causes: list = []
        valuer.value_on(d, {k: v["qty"] for k, v in qty_src.items()}, anchor=anchor, causes=causes)
        for c in causes:
            out["legs"].append({**c, **facts((c["ticker"], c["currency"]))})
    if plan.self_check is not None and plan.self_check > _SELF_CHECK_TOLERANCE:
        out["reprice"] = [_reprice_detail(snap, anchor, agg, valuer)
                          for snap, anchor, agg in ((a, "A", a_agg), (b, "B", b_agg))]
    return out


def _reprice_detail(snap, anchor: str, agg: dict, valuer: _Valuer) -> dict:
    """``--explain`` for a failed self-check: each leg's recorded value vs its repriced value
    at one end, largest gap first, so a residual can be located."""
    got = valuer.value_on(snap.snap_date, {k: v["qty"] for k, v in agg.items()}, anchor=anchor)
    repriced = {} if got is None else {(x["ticker"], x["currency"]): x for x in got[1]}
    nav = float(snap.nav or 0.0)
    legs = []
    for k, rec in agg.items():
        x = repriced.get(k)
        value = None if x is None else x["value"]
        legs.append({"ticker": k[0], "currency": k[1], "qty": rec["qty"], "recorded_price": rec["price"],
                     "repriced_price": None if x is None else x["price"],
                     "recorded_value": round(rec["value"], 2), "repriced_value": None if value is None else round(value, 2),
                     "diff": None if value is None else round(value - rec["value"], 2),
                     "diff_pct_of_nav": None if value is None or not nav else round((value - rec["value"]) / nav * 100, 4)})
    legs.sort(key=lambda x: -abs(x["diff"] or 0.0))
    total = None if got is None else got[0]
    return {"anchor": anchor, "date": snap.snap_date.isoformat(), "recorded_nav": round(nav, 2),
            "recorded_legs_sum": round(sum(v["value"] for v in agg.values()), 2),
            "repriced_nav": None if total is None else round(total, 2),
            "rel_error_pct": None if total is None or not nav else round((total / nav - 1) * 100, 4), "legs": legs}


def _restate_anchor(row, history, plan: SeriesPlan, done: set[int]) -> None:
    """Restate one still-provisional anchor row in memory (``performance._restate_provisional``)
    and remember its prior state for the stash. Committed only by ``apply_plan``."""
    if row is None or not row.provisional or id(row) in done:
        return
    done.add(id(row))
    prior_comp = copy.deepcopy(row.composition or {})
    prior = {"nav": float(row.nav), "provisional": bool(row.provisional), "legs": prior_comp.get("legs") or []}
    if performance._restate_provisional(row, history):
        # _restate_provisional rewrites composition as {schema, legs}; keep any other keys.
        row.composition = {**prior_comp, **row.composition}
        plan.restated.append((row, prior, {"date": row.snap_date.isoformat(), "nav": float(row.nav),
                                           "provisional": bool(row.provisional)}))


def _ratio_terms(ratio: float) -> Fraction | None:
    """``ratio`` as a simple fraction (both terms ≤ ``_SPLIT_MAX_TERM``), or None."""
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    frac = Fraction(ratio).limit_denominator(_SPLIT_MAX_TERM)
    if frac.numerator > _SPLIT_MAX_TERM or min(frac.numerator, frac.denominator) > _SPLIT_MAX_SMALL_TERM \
            or not _same_qty(float(frac), ratio):
        return None
    return frac


def _near(x: float, want: float, tol: float = _SPLIT_PRICE_TOLERANCE) -> bool:
    return want > 0 and abs(x / want - 1) <= tol


def _split(k, qa: float, qb: float, a, b, a_agg, b_agg, ctx: _Ctx) -> dict | str:
    """The split that explains ``qa → qb`` with no trade, or why there is none.

    Never invented: the leg's close series must show it, either as a one-day step of
    ``1/ratio`` inside ``(A, B]`` (unadjusted series) or as a stored close at A that is
    A's recorded price divided by the ratio, with B's matching and no step (adjusted)."""
    if not qa or not qb:
        return "not held at both ends"
    ratio = qb / qa
    frac = _ratio_terms(ratio)
    if frac is None:
        return f"quantity ratio {ratio:.6g} is not a simple split ratio"
    if abs(math.log(ratio)) < _SPLIT_MIN_MOVE:
        return f"quantity ratio {ratio:.6g} is too small to tell from a day's price move"
    terms = f"{frac.numerator}:{frac.denominator}"
    recorded = sorted((ctx.corporate or {}).get("splits", {}).get(k[0], []))
    if recorded and not any(_same_qty(r, ratio) for _d, r in recorded):
        return f"recorded split ratio {recorded[0][1]:g} does not match the quantity ratio {terms}"
    series = ctx.history.get(k[0]) or []
    bar_a, bar_b = _asof(series, a.snap_date), _asof(series, b.snap_date)
    a_px, b_px = a_agg[k]["price"], b_agg[k]["price"]
    if bar_a is None or bar_b is None or not bar_a.close or not a_px or not b_px:
        return "no close history (or recorded price) at A and B to confirm a split"
    steps = []
    for i, bar in enumerate(series):
        if i and a.snap_date < bar.bar_date <= b.snap_date and series[i - 1].close:
            if _near(bar.close / series[i - 1].close, 1 / ratio):
                steps.append(bar.bar_date)
    note = {"kind": "split", "ticker": k[0], "ratio": ratio, "terms": terms, "a_qty": qa, "b_qty": qb,
            "recorded_date": recorded[0][0].isoformat() if recorded else None}
    if len(steps) == 1 and _near(a_px, bar_a.close):
        return {**note, "date": steps[0].isoformat(), "basis": "unadjusted", "step": steps[0]}
    if not steps and _near(a_px, bar_a.close * ratio) and _near(b_px, bar_b.close):
        # Valued at b_qty × the adjusted close every day: the same NAV whatever the date.
        return {**note, "date": note["recorded_date"], "basis": "adjusted", "step": None}
    if len(steps) > 1:
        return f"{len(steps)} sessions step by 1/{terms}; the split date is ambiguous"
    return f"no close-to-close step of 1/{terms} inside the gap (and the closes are not split-adjusted)"


def _drip(k, qa: float, qb: float, a, ctx: _Ctx) -> dict | str:
    """A small reinvested dividend explaining ``qa → qb`` with no trade, or why not."""
    if not qa or qb <= qa:
        return "not an increase"
    frac = (qb - qa) / qa
    if frac > _DRIP_MAX_FRACTION:
        return f"increase of {frac:.2%} exceeds the {_DRIP_MAX_FRACTION:.0%} reinvestment bound"
    recorded = sorted((ctx.corporate or {}).get("dividends", {}).get(k[0], []))
    step = recorded[-1] if recorded else a.snap_date + timedelta(days=1)
    return {"kind": "drip", "ticker": k[0], "a_qty": qa, "b_qty": qb, "fraction": frac,
            "date": step.isoformat(), "date_source": "ledger-dividend" if recorded else "first-day-after-A",
            "step": step}


def _reconcile(keys, a_agg, b_agg, trades, by_ticker, a, b, ctx: _Ctx):
    """``(adjustments, mismatches)`` for one trade source: every leg must close A + trades =
    B exactly, or (under trade_aware, with no trade in that leg) by a split or a DRIP."""
    net: dict = defaultdict(float)
    traded: set = set()
    for _d, ticker, q in trades:
        ks = by_ticker.get(ticker) or []
        if len(ks) != 1:
            return [], [{"ticker": ticker, "reason": "traded leg held at neither end" if not ks else "ambiguous currency"}]
        net[ks[0]] += q
        traded.add(ks[0])
    adjustments, mismatches = [], []
    for k in sorted(keys):
        qa = a_agg[k]["qty"] if k in a_agg else 0.0
        qb = b_agg[k]["qty"] if k in b_agg else 0.0
        if _same_qty(qa + net[k], qb):
            continue
        miss = {"ticker": k[0], "a_qty": qa, "traded": net[k], "b_qty": qb}
        if k in traded:
            mismatches.append(miss)
            continue
        found = _split(k, qa, qb, a, b, a_agg, b_agg, ctx)
        if isinstance(found, str):
            why_split = found
            found = _drip(k, qa, qb, a, ctx)
            if isinstance(found, str):
                mismatches.append({**miss, "not_split": why_split, "not_drip": found})
                continue
        adjustments.append((k, found))
    return adjustments, mismatches


def _trade_quantities(plan: SeriesPlan, a, b, a_agg, b_agg, ctx: _Ctx):
    """Pick the trade source that reconciles A to B exactly for every leg (a leg with no
    trade may be closed by a split or a DRIP). Returns ``(source, trades, adjustments)`` or
    sets ``plan.skipped`` and returns None."""
    keys = set(a_agg) | set(b_agg)
    by_ticker: dict[str, list] = defaultdict(list)
    for k in keys:
        by_ticker[k[0]].append(k)
    tried = []
    sources = [(src, t) for src, t in (ctx.trades or {}).items() if t]
    # No trade at all: the corporate events alone may still close every leg.
    for source, trades in [*sources, ("none", [])]:
        adjustments, mismatches = _reconcile(keys, a_agg, b_agg, trades, by_ticker, a, b, ctx)
        if not mismatches and (trades or adjustments):
            return source, [(d, by_ticker[t][0], q) for d, t, q in trades], adjustments
        if trades or not sources:
            tried.append({"source": source, "mismatches": mismatches})
    plan.notes.append({"kind": "trade-sources", "tried": tried})
    plan.skipped = "no trade record (ledger or lots) reconciles A to B for every leg"
    return None


def _settle_flows(plan: SeriesPlan, b, flow_of) -> None:
    """Flow conservation for a plan whose inserted rows carry flows (metron-ops-I361 (iii)).

    B's live flow spans ``(P, B]``, P the last row that existed before B. Inserted rows after
    P now take part of that flow, so B is rebased to ``(last inserted day, B]`` — the live
    writer's own definition — and the inserted flows plus the rebased flow must equal B's
    original to the cent. An inserted flow followed by a row that already existed inside the
    gap (another writer's row whose flow already spans that day) would count twice: refused."""
    timeline = sorted(set(plan.points) | set(plan.existing_in_gap))
    for i, d in enumerate(timeline):
        if d in plan.points and abs(plan.flows.get(d, 0.0)) > _CENT:
            nxt = timeline[i + 1] if i + 1 < len(timeline) else None
            if nxt is not None and nxt not in plan.points:
                plan.skipped = (f"a flow on {d.isoformat()} precedes an existing row on {nxt.isoformat()} "
                                "whose flow already spans it")
                return
    p = max(plan.existing_in_gap, default=None)
    after_p = [d for d in plan.points if p is None or d > p]
    if not after_p:
        return
    original = float(b.external_flow or 0.0)
    rebased = flow_of(max(after_p), b.snap_date)
    inserted = sum(plan.flows.get(d, 0.0) for d in after_p)
    if abs(inserted + rebased - original) > _CENT:
        plan.skipped = (f"flow conservation fails: inserted {inserted:.2f} + rebased {rebased:.2f} "
                        f"!= recorded {original:.2f} on {b.snap_date.isoformat()}")
        return
    if abs(rebased - original) > _CENT:
        plan.rebase = (b, original, rebased)


def _fail(plan: SeriesPlan, reason: str) -> None:
    plan.skipped = reason
    plan.points.clear()
    plan.legs.clear()
    plan.flows.clear()
    plan.spy.clear()
    plan.estimated_legs.clear()
    plan.rebase = None


def _plan_series(plan: SeriesPlan, a, b, history, fx_hist, ctx: _Ctx | None = None) -> None:
    """Fill ``plan.points`` for ``plan.missing`` from A/B compositions, or set ``skipped``."""
    ctx = ctx or _Ctx(history, fx_hist)
    _plan_series_core(plan, a, b, ctx)
    if not ctx.opts.explain:
        return
    a_agg = _aggregate((a.composition or {}).get("legs") or []) if a is not None else {}
    b_agg = _aggregate((b.composition or {}).get("legs") or []) if b is not None else {}
    if plan.skipped:
        plan.explain = _explain(plan, a, b, a_agg, b_agg, ctx)
    broker = _broker_legs(a, b, a_agg, b_agg, ctx.history, ctx.opts, plan.missing)
    if broker:
        plan.explain = {**(plan.explain or {}), "broker_legs": broker}


def _plan_series_core(plan: SeriesPlan, a, b, ctx: _Ctx) -> None:
    opts = ctx.opts
    if a is None or b is None:
        plan.skipped = "no snapshot on one side of the gap"
        return
    a_legs = (a.composition or {}).get("legs") or []
    b_legs = (b.composition or {}).get("legs") or []
    if not a_legs or not b_legs:
        plan.skipped = "no recorded composition to value from"
        return
    a_agg, b_agg = _aggregate(a_legs), _aggregate(b_legs)
    same = set(a_agg) == set(b_agg) and all(_same_qty(a_agg[k]["qty"], bl["qty"]) for k, bl in b_agg.items())
    trades: list[tuple[date, tuple, float]] = []
    adjustments: list[tuple[tuple, dict]] = []
    aware = False
    if same or not opts.trade_aware:
        if set(a_agg) != set(b_agg):
            plan.skipped = "holdings differ across the gap"
            return
        for k, bl in b_agg.items():
            if not _same_qty(a_agg[k]["qty"], bl["qty"]):
                plan.skipped = f"quantity of {k[0]} changed inside the gap"
                return
        refs = b_agg
    else:
        picked = _trade_quantities(plan, a, b, a_agg, b_agg, ctx)
        if picked is None:
            return
        source, trades, adjustments = picked
        aware = True
        plan.method = "trade-aware" + "".join(sorted({f"+{adj['kind']}" for _k, adj in adjustments}))
        plan.notes.append({"kind": "trade-source", "source": source,
                           "trades": [{"date": d.isoformat(), "ticker": k[0], "qty": q} for d, k, q in trades]})
        plan.notes.extend({x: v for x, v in adj.items() if x != "step"} for _k, adj in adjustments)
        refs = {}
        for k in list(b_agg) + [k for k in a_agg if k not in b_agg]:
            bl = b_agg.get(k)
            refs[k] = bl if bl is not None and bl["qty"] and bl["value"] else a_agg[k]

    def corporate(d: date, q: dict) -> dict:
        # A split on an adjusted series is valued in post-split shares on every day; an
        # unadjusted split or a DRIP steps a_qty -> b_qty on its day. Neither is a flow.
        for k, adj in adjustments:
            q[k] = adj["b_qty"] if adj["step"] is None or adj["step"] <= d else adj["a_qty"]
        return q

    def qty_on(d: date) -> dict:
        q = {k: (a_agg[k]["qty"] if k in a_agg else 0.0) for k in refs}
        for td, k, dq in trades:
            if td <= d:
                q[k] += dq
        return corporate(d, q)

    valuer = _Valuer(a, b, a_agg, b_agg, refs, ctx.history, ctx.fx_hist, opts)
    if opts.estimate_broker_legs:
        broker = _broker_legs(a, b, a_agg, b_agg, ctx.history, opts, plan.missing)
        refused = [x for x in broker if x["verdict"] == "refuse"]
        if refused:
            plan.skipped = "refusing to interpolate: " + "; ".join(x["reason"] for x in refused)
            return
        valuer.interp = frozenset((x["ticker"], x["currency"]) for x in broker if x["verdict"] == "interpolate")
    errors, scales = [], []
    for snap, anchor in ((a, "A"), (b, "B")):
        qty = {k: v["qty"] for k, v in (a_agg if anchor == "A" else b_agg).items()} if aware \
            else {k: v["qty"] for k, v in b_agg.items()}
        if aware:
            qty = corporate(snap.snap_date, qty)
        got = valuer.value_on(snap.snap_date, qty, anchor=anchor)
        if got is None:
            plan.skipped = f"cannot re-price the {snap.snap_date.isoformat()} snapshot (missing close or FX)"
            return
        nav = float(snap.nav)
        if not nav:
            plan.skipped = f"the {snap.snap_date.isoformat()} snapshot has zero NAV"
            return
        errors.append(abs(got[0] - nav) / nav)
        scales.append(nav / got[0] if got[0] else float("inf"))
    plan.self_check = max(errors)
    scale_of = None
    if plan.self_check > _SELF_CHECK_TOLERANCE:
        if opts.anchor != "proportional" or plan.self_check > _ANCHOR_MAX_ERROR:
            plan.skipped = "pricing method does not reproduce the bracketing snapshots' recorded NAV"
            if opts.anchor == "proportional":
                plan.skipped += f" (and an end is off by more than the {_ANCHOR_MAX_ERROR:.0%} anchoring bound)"
            return
        span = (b.snap_date - a.snap_date).days

        def scale_of(d: date) -> float:
            return scales[0] + (scales[1] - scales[0]) * (d - a.snap_date).days / span

        plan.notes.append({"kind": "anchor", "method": "proportional", "error_a": errors[0], "error_b": errors[1],
                           "scale_a": scales[0], "scale_b": scales[1]})
    plan.cost_basis = float(a.cost_basis or 0.0)
    spy = ctx.history.get("SPY")
    for d in plan.missing:
        got = valuer.value_on(d, qty_on(d) if aware else {k: v["qty"] for k, v in b_agg.items()})
        if got is None:
            _fail(plan, f"no close for a held leg on {d.isoformat()}")
            return
        if scale_of is not None:
            got = _scaled(got, d, scale_of(d))
        plan.points[d], plan.legs[d], est = got
        plan.estimated_legs.extend(est)
        bar = _asof(spy, d)
        plan.spy[d] = bar.close if bar else None
        carried = sum(e["value"] for e in est if e["how"] == "carry")
        if carried > _CARRY_CAP * plan.points[d]:
            _fail(plan, f"carried legs exceed {_CARRY_CAP:.0%} of NAV on {d.isoformat()}")
            return
        bridged = sum(e["value"] for e in est if e["how"] == "proxy-bridge")
        if bridged > _BRIDGE_CAP * plan.points[d]:
            _fail(plan, f"proxy-bridged leg exceeds {_BRIDGE_CAP:.0%} of NAV on {d.isoformat()}")
            return
    if plan.estimated_legs:
        plan.method += "+" + "+".join(sorted({e["how"] for e in plan.estimated_legs}))
    if aware:
        steps = {adj["step"] for _k, adj in adjustments if adj["step"] is not None}
        _trade_flows(plan, a, b, {d for d, _k, _q in trades} | steps, ctx.flow_of)


def _scaled(got, d: date, f: float):
    """One repriced day scaled by the proportional anchor ``f`` (metron-ops-I361 D3)."""
    total, legs, estimated = got
    legs = [{**leg, "value": leg["value"] * f, "anchor_scale": f} for leg in legs]
    estimated = [*estimated, {"ticker": None, "date": d.isoformat(), "how": "anchor", "value": total * (f - 1),
                              "price_date": None, "proxy": None, "scale": f}]
    return total * f, legs, estimated


def _trade_flows(plan: SeriesPlan, a, b, trade_days: set[date], flow_of) -> None:
    """Live-writer flows on the inserted rows, B's rebase, and the trade-day sanity check."""
    timeline = sorted({a.snap_date} | set(plan.points) | set(plan.existing_in_gap))
    for i, d in enumerate(timeline[1:], start=1):
        if d in plan.points:
            plan.flows[d] = flow_of(timeline[i - 1], d)
    if not any(abs(v) > _CENT for v in plan.flows.values()):
        plan.notes.append({"kind": "no-ledger-flow",
                           "text": "the trades carry no ledger flow inside the gap; B keeps its recorded flow"})
    _settle_flows(plan, b, flow_of)
    if plan.skipped:
        _fail(plan, plan.skipped)
        return
    navs = {a.snap_date: float(a.nav), **plan.existing_in_gap, **plan.points, b.snap_date: float(b.nav)}
    order = sorted(navs)
    for i, d in enumerate(order[1:], start=1):
        if d not in trade_days:
            continue
        if d == b.snap_date:
            flow = plan.rebase[2] if plan.rebase else float(b.external_flow or 0.0)
        else:
            flow = plan.flows.get(d, 0.0)
        prev = navs[order[i - 1]]
        r = (navs[d] - flow) / prev - 1 if prev else float("inf")
        if abs(r) >= _TRADE_DAY_RETURN_CAP:
            _fail(plan, f"implausible {r:+.1%} return on trade day {d.isoformat()} (sign or quantity error?)")
            return


def _plan_from_accounts(plan: SeriesPlan, a, b, acct_rows: list, acct_plans: list[SeriesPlan], flow_of) -> None:
    """Portfolio-grain missed days as the sum of the portfolio's accounts (metron-ops-I361 D5)."""
    plan.method = "sum-of-accounts"
    if a is None or b is None:
        plan.skipped = "no snapshot on one side of the gap"
        return
    by_date: dict[date, dict] = defaultdict(dict)
    for r in acct_rows:
        meta = (r.composition or {}).get("backfill") or {}
        by_date[r.snap_date][r.account_id] = {
            "nav": float(r.nav), "flow": float(r.external_flow or 0.0), "cost": float(r.cost_basis or 0.0),
            "spy": None if r.spy_close is None else float(r.spy_close),
            "legs": (r.composition or {}).get("legs") or [],
            "estimated_legs": meta.get("estimated_legs") or [],
        }
    for ap in acct_plans:
        if ap.skipped:
            continue
        for d, nav in ap.points.items():
            iso = d.isoformat()
            by_date[d].setdefault(ap.account_id, {
                "nav": nav, "flow": ap.flows.get(d, 0.0), "cost": ap.cost_bases.get(d, ap.cost_basis),
                "spy": ap.spy.get(d), "legs": ap.legs.get(d, []),
                "estimated_legs": [e for e in ap.estimated_legs if e["date"] == iso],
            })
    cohort = set(by_date.get(a.snap_date, {}))
    if not cohort:
        plan.skipped = "no account rows at the start of the gap"
        return
    if cohort != set(by_date.get(b.snap_date, {})):
        plan.skipped = "the account cohort differs between the two ends of the gap"
        return
    ends = [_cohort_identity(snap, end, by_date[snap.snap_date]) for snap, end in ((a, "A"), (b, "B"))]
    bad = [e for e in ends if e["rel_err"] > _COHORT_IDENTITY_TOLERANCE]
    if bad:
        detail = "; ".join(_identity_text(e) for e in ends)
        plan.skipped = (f"the accounts do not sum to the portfolio on {bad[0]['date']} "
                        f"(cohort of {len(cohort)} accounts; {detail})")
        return
    for e in ends:
        if e["rewritten"]:
            plan.notes.append({"kind": "reconstructed-anchor", "end": e["end"], "date": e["date"],
                               "recorded_nav": round(e["nav"], 2), "legs_sum": round(e["legs_sum"], 2),
                               "residual": round(e["nav"] - e["legs_sum"], 2)})
    plan.self_check = max(e["rel_err"] for e in ends)
    for d, recorded in sorted(plan.existing_in_gap.items()):
        present = by_date.get(d, {})
        if set(present) == cohort and recorded:
            total = sum(c["nav"] for c in present.values())
            plan.notes.append({"kind": "existing-row-vs-account-sum", "date": d.isoformat(),
                               "recorded": round(recorded, 2), "account_sum": round(total, 2),
                               "rel_diff": round(total / recorded - 1, 6)})
    for d in plan.missing:
        present = by_date.get(d, {})
        if set(present) != cohort:
            lacking = sorted(str(x) for x in cohort - set(present))
            outside = sorted(str(x) for x in set(present) - cohort)
            plan.notes.append({"kind": "cohort", "date": d.isoformat(), "lacking": lacking, "outside": outside})
            _fail(plan, f"not every account of the cohort has a row on {d.isoformat()}")
            return
        iso = d.isoformat()
        plan.points[d] = sum(c["nav"] for c in present.values())
        plan.flows[d] = sum(c["flow"] for c in present.values())
        plan.cost_bases[d] = sum(c["cost"] for c in present.values())
        plan.spy[d] = next((c["spy"] for c in present.values() if c["spy"] is not None), None)
        plan.legs[d] = [leg for _aid, c in sorted(present.items(), key=lambda kv: str(kv[0])) for leg in c["legs"]]
        for aid, c in sorted(present.items(), key=lambda kv: str(kv[0])):
            plan.estimated_legs.extend({**e, "date": iso, "account_id": str(aid)} for e in c["estimated_legs"])
    plan.cost_basis = float(a.cost_basis or 0.0)
    _settle_flows(plan, b, flow_of)
    if plan.skipped:
        _fail(plan, plan.skipped)


def _cohort_identity(snap, end: str, present: dict) -> dict:
    """Σ the cohort's account rows vs the portfolio row at one end of the gap.

    The portfolio row is compared on the live writer's basis. ``record_snapshot`` writes
    ``nav == Σ composition legs`` by construction, and so does every other writer of a
    composition (restatement, this module). ``reconstruct_snapshots`` — re-run on every
    daily refresh — upserts each month-end and flow date's ``nav`` from the lot timeline
    WITHOUT touching the composition, so such a row carries the reconstructed NAV over the
    live legs: ``nav = Σ legs + X``, X recorded by the row itself. Account rows are never
    rewritten that way, and a missed day is by definition one reconstruct did not write,
    so the accounts are compared with Σ legs and the inserted days stay a plain sum."""
    total = sum(c["nav"] for c in present.values())
    nav = float(snap.nav)
    legs = (snap.composition or {}).get("legs") or []
    legs_sum = sum(float(x.get("value") or 0.0) for x in legs)
    rewritten = bool(legs) and abs(nav - legs_sum) > _CENT
    target = legs_sum if rewritten else nav
    rel_err = abs(total - target) / target if target else float("inf")
    return {"end": end, "date": snap.snap_date.isoformat(), "accounts": total, "nav": nav,
            "legs_sum": legs_sum, "rewritten": rewritten, "target": target, "rel_err": rel_err}


def _identity_text(e: dict) -> str:
    diff = e["accounts"] - e["target"]
    pct = f"{diff / e['target'] * 100:+.4f}%" if e["target"] else "n/a"
    text = (f"{e['end']} {e['date']}: accounts {e['accounts']:.2f} vs portfolio {e['target']:.2f}, "
            f"diff {diff:+.2f} ({pct})")
    if e["rewritten"]:
        text += f" [portfolio legs sum; row NAV {e['nav']:.2f} was rewritten by reconstruction]"
    return text


def _trade_records(session: Session, plan: SeriesPlan, a, b) -> dict[str, list[tuple[date, str, float]]]:
    """Signed quantities traded in ``(A, B]`` from the ledger and from the broker lots."""
    if plan.account_id is not None:
        accounts = [plan.account_id]
    else:
        accounts = list(session.scalars(select(models.Account.id).where(
            models.Account.tenant_id == plan.tenant_id, models.Account.portfolio_id == plan.portfolio_id)))
    lo, hi = a.snap_date, b.snap_date
    txn, opened, closed = models.Transaction, models.OpenLot, models.RealizedLot
    ledger = [
        (d, sym, (-1.0 if t == TxnType.SELL.value else 1.0) * abs(float(q)))
        for d, t, q, sym in session.execute(
            select(txn.trade_date, txn.txn_type, txn.quantity, models.Security.symbol)
            .join(models.Security, txn.security_id == models.Security.id)
            .where(txn.tenant_id == plan.tenant_id, txn.account_id.in_(accounts), txn.trade_date > lo,
                   txn.trade_date <= hi, txn.txn_type.in_(performance._NET_PURCHASE_TYPE_VALUES))
        ).all()
    ]
    lots = [(d, t, abs(float(q))) for d, t, q in session.execute(
        select(opened.open_date, opened.ticker, opened.quantity).where(
            opened.tenant_id == plan.tenant_id, opened.account_id.in_(accounts),
            opened.open_date > lo, opened.open_date <= hi)
    ).all()]
    # A closed lot is held from its open date to its close date: it adds inside the gap if
    # it opened there (whenever it closed) and subtracts if it closed there.
    for od, cd, t, q in session.execute(
        select(closed.open_date, closed.close_date, closed.ticker, closed.quantity).where(
            closed.tenant_id == plan.tenant_id, closed.account_id.in_(accounts),
            or_(and_(closed.open_date > lo, closed.open_date <= hi),
                and_(closed.close_date > lo, closed.close_date <= hi)))
    ).all():
        if lo < od <= hi:
            lots.append((od, t, abs(float(q))))
        if lo < cd <= hi:
            lots.append((cd, t, -abs(float(q))))
    key = itemgetter(0, 1)
    return {"ledger": sorted(ledger, key=key), "lots": sorted(lots, key=key)}


def _corporate_records(session: Session, plan: SeriesPlan, a, b) -> dict:
    """Recorded SPLITs (ratio) and DIVIDENDs per ticker in ``(A, B]`` for this series."""
    if plan.account_id is not None:
        accounts = [plan.account_id]
    else:
        accounts = list(session.scalars(select(models.Account.id).where(
            models.Account.tenant_id == plan.tenant_id, models.Account.portfolio_id == plan.portfolio_id)))
    txn = models.Transaction
    out: dict = {"splits": defaultdict(list), "dividends": defaultdict(list)}
    for d, t, q, sym in session.execute(
        select(txn.trade_date, txn.txn_type, txn.quantity, models.Security.symbol)
        .join(models.Security, txn.security_id == models.Security.id)
        .where(txn.tenant_id == plan.tenant_id, txn.account_id.in_(accounts), txn.trade_date > a.snap_date,
               txn.trade_date <= b.snap_date, txn.txn_type.in_([TxnType.SPLIT.value, TxnType.DIVIDEND.value]))
    ).all():
        if t == TxnType.SPLIT.value:
            out["splits"][sym].append((d, float(q)))
        else:
            out["dividends"][sym].append(d)
    return out


def _flow_reader(session: Session, plan: SeriesPlan) -> Callable[[date, date], float]:
    """The live writer's own flow definition for this series, over ``(after, through]``."""
    if plan.account_id is not None:
        return lambda after, through: performance._account_net_purchases(
            session, plan.tenant_id, plan.account_id, after=after, through=through)
    return lambda after, through: performance._net_purchases(
        session, plan.tenant_id, plan.portfolio_id, after=after, through=through)


def plan_gap(
    session: Session, *, start: date, end: date, exclude_portfolios: set[uuid.UUID] = frozenset(),
    options: Options | None = None,
) -> list[SeriesPlan]:
    """Read-only: every portfolio and account series with a missing day in [start, end],
    each with its proposed NAV per missing day or the reason it is skipped.

    With ``options.restate_anchors`` the bracketing rows are restated IN MEMORY only; they
    reach the database solely through :func:`apply_plan`, so a caller doing a dry run must
    roll the session back (the CLI does)."""
    opts = options or Options()
    plans: list[tuple[SeriesPlan, object, object]] = []
    days = _days(start, end)
    grains = (
        ("portfolio", models.NavSnapshot, lambda r: (r.portfolio_id, None)),
        ("account", models.AccountNavSnapshot, lambda r: (r.portfolio_id, r.account_id)),
    )
    for grain, model, key_of in grains:
        series_key = model.portfolio_id if grain == "portfolio" else model.account_id
        rows = session.scalars(
            select(model).where(
                model.snap_date >= start - timedelta(days=_HISTORY_LOOKBACK_DAYS),
            ).order_by(series_key, model.snap_date)
        ).all()
        by_series: dict[tuple, list] = defaultdict(list)
        for r in rows:
            if r.portfolio_id in exclude_portfolios:
                continue
            by_series[(r.tenant_id, *key_of(r))].append(r)
        for (tenant_id, portfolio_id, account_id), srows in by_series.items():
            have = {r.snap_date for r in srows}
            missing = [d for d in days if d not in have]
            if not missing:
                continue
            a = next((r for r in reversed(srows) if r.snap_date < start), None)
            b = next((r for r in srows if r.snap_date > end), None)
            plan = SeriesPlan(grain, tenant_id, portfolio_id, account_id, missing)
            if not opts.selects(plan):
                continue
            if a is not None and b is not None:
                plan.existing_in_gap = {
                    r.snap_date: float(r.nav) for r in srows if a.snap_date < r.snap_date < b.snap_date
                }
            plans.append((plan, a, b))

    # Account rows bracketing each portfolio gap, for --portfolio-from-accounts.
    acct_rows: dict[uuid.UUID, list] = {}
    if opts.portfolio_from_accounts or opts.explain:
        for plan, a, b in plans:
            if plan.grain == "portfolio" and a is not None and b is not None:
                acct_rows[plan.portfolio_id] = list(session.scalars(
                    select(models.AccountNavSnapshot).where(
                        models.AccountNavSnapshot.tenant_id == plan.tenant_id,
                        models.AccountNavSnapshot.portfolio_id == plan.portfolio_id,
                        models.AccountNavSnapshot.snap_date >= a.snap_date,
                        models.AccountNavSnapshot.snap_date <= b.snap_date,
                    )
                ).all())
    anchors = [snap for _p, a, b in plans for snap in (a, b) if snap is not None]
    if opts.portfolio_from_accounts:
        anchors += [r for rows in acct_rows.values() for r in rows]

    tickers = sorted({"SPY"} | set(opts.proxy_bridge.values()) | {
        leg["ticker"]
        for snap in anchors if snap.composition
        for leg in snap.composition.get("legs") or []
    })
    ccys = sorted({
        (leg.get("currency") or "USD").upper()
        for snap in anchors if snap.composition
        for leg in snap.composition.get("legs") or []
    })
    last_b = max((b.snap_date for _p, _a, b in plans if b is not None), default=end)
    history = price_service.close_history_by_symbol(
        session, tickers, start_date=start - timedelta(days=_HISTORY_LOOKBACK_DAYS), end_date=last_b
    ) if tickers else {}
    fx_hist = fx_service.rate_history(session, ccys)
    if opts.leg_prices:
        _check_leg_prices(opts, plans, history)

    if opts.restate_anchors:
        done: set[int] = set()
        for plan, a, b in plans:
            for row in (a, b):
                _restate_anchor(row, history, plan, done)
            if plan.grain == "portfolio" and a is not None and b is not None and opts.portfolio_from_accounts:
                for row in acct_rows.get(plan.portfolio_id, []):
                    if row.snap_date in (a.snap_date, b.snap_date):
                        _restate_anchor(row, history, plan, done)

    # Accounts first: a portfolio summed from its accounts reads their planned points.
    for plan, a, b in sorted(plans, key=lambda t: t[0].grain != "account"):
        ctx = _Ctx(history, fx_hist, opts)
        if plan.grain == "portfolio" and opts.portfolio_from_accounts:
            _plan_from_accounts(
                plan, a, b, acct_rows.get(plan.portfolio_id, []),
                [p for p, _a, _b in plans if p.grain == "account" and p.portfolio_id == plan.portfolio_id],
                _flow_reader(session, plan),
            )
            continue
        if opts.trade_aware and a is not None and b is not None:
            ctx.trades = _trade_records(session, plan, a, b)
            ctx.corporate = _corporate_records(session, plan, a, b)
            ctx.flow_of = _flow_reader(session, plan)
        _plan_series(plan, a, b, history, fx_hist, ctx)
    if opts.explain:
        for plan, a, b in plans:
            if plan.grain == "portfolio" and plan.skipped and a is not None and b is not None:
                if plan.explain is None:
                    plan.explain = _explain(plan, a, b, {}, {}, _Ctx(history, fx_hist, opts))
                plan.explain["account_sum"] = _account_sum(a, b, acct_rows.get(plan.portfolio_id, []))
    return [p for p, _a, _b in plans]


def _account_sum(a, b, acct_rows: list) -> list[dict]:
    """``--explain`` for a portfolio: Σ its accounts' NAVs vs its own row, at A and at B."""
    out = []
    for snap in (a, b):
        navs = sorted((str(r.account_id), float(r.nav)) for r in acct_rows if r.snap_date == snap.snap_date)
        total, nav = sum(v for _i, v in navs), float(snap.nav)
        out.append({"date": snap.snap_date.isoformat(), "portfolio_nav": round(nav, 2),
                    "account_sum": round(total, 2), "diff": round(total - nav, 2),
                    "rel_diff_pct": round((total / nav - 1) * 100, 4) if nav else None,
                    "accounts": [{"account_id": i, "nav": round(v, 2)} for i, v in navs]})
    return out


def _check_leg_prices(opts: Options, plans, history) -> None:
    """Every ``leg_prices`` ticker must be held by a planned series, and priced on every US
    session inside each gap where it is held — or the run fails, naming what is missing.
    Under ``estimate_broker_legs`` an unsupplied day of an interpolable leg is estimated."""
    sessions = {p.bar_date for p in history.get("SPY") or []}
    held = set()
    for plan, a, b in plans:
        if a is None or b is None:
            continue
        tickers = {leg["ticker"] for snap in (a, b) for leg in (snap.composition or {}).get("legs") or []}
        interpolable = set()
        if opts.estimate_broker_legs:
            a_agg = _aggregate((a.composition or {}).get("legs") or [])
            b_agg = _aggregate((b.composition or {}).get("legs") or [])
            interpolable = {x["ticker"] for x in _broker_legs(a, b, a_agg, b_agg, history, opts, plan.missing)
                            if x["verdict"] in ("interpolate", "refuse")}
        for ticker in tickers & set(opts.leg_prices):
            held.add(ticker)
            lacking = [d for d in plan.missing if d in sessions and d not in opts.leg_prices[ticker]]
            if lacking and ticker not in interpolable:
                raise LegPricesError(f"--leg-prices has no price for {ticker} on "
                                     f"{', '.join(d.isoformat() for d in lacking)} (US sessions in the gap)")
    unused = sorted(set(opts.leg_prices) - held)
    if unused:
        raise LegPricesError(f"--leg-prices names tickers no gapped series holds: {', '.join(unused)}")


def apply_plan(session: Session, plans: list[SeriesPlan], *, options: Options | None = None) -> int:
    """Insert one row per planned (series, day). Never overwrites an existing row. Commits.

    Under any opt-in option the rows also carry ``method``/``estimated``/``estimated_legs``,
    restated anchors and a rebased B flow are persisted with their prior values stashed,
    and a series with a flow to rebase is written whole or not at all."""
    extended = (options or Options()).extended
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    written = 0
    for plan in plans:
        gap = [min(plan.missing).isoformat(), max(plan.missing).isoformat()] if plan.missing else None
        for row, prior, _after in plan.restated:
            row.composition = {**(row.composition or {}), "backfill_restate": {
                "by": BACKFILL_MARKER, "at": stamp, "gap": gap, **prior}}
        if plan.skipped or not plan.points:
            continue
        model = models.NavSnapshot if plan.grain == "portfolio" else models.AccountNavSnapshot
        owner = (model.portfolio_id == plan.portfolio_id) if plan.grain == "portfolio" \
            else (model.account_id == plan.account_id)
        extra = {"account_id": plan.account_id} if plan.grain == "account" else {}
        taken = {d for d in plan.points if session.scalars(
            select(model.id).where(model.tenant_id == plan.tenant_id, owner, model.snap_date == d)
        ).first() is not None}
        if taken and (plan.rebase is not None or any(plan.flows.values())):
            logger.warning("backfill-snapshot-gap: %s %s changed since planning; not written",
                           plan.grain, plan.account_id or plan.portfolio_id)
            continue
        for d, nav in sorted(plan.points.items()):
            if d in taken:
                continue  # a live writer filled it since the plan was made
            meta = {"by": BACKFILL_MARKER, "at": stamp}
            if extended:
                day_legs = [e for e in plan.estimated_legs if e["date"] == d.isoformat()]
                meta.update(method=plan.method, estimated=bool(day_legs), estimated_legs=day_legs)
            session.add(model(
                tenant_id=plan.tenant_id, portfolio_id=plan.portfolio_id, snap_date=d,
                nav=nav, cost_basis=plan.cost_bases.get(d, plan.cost_basis),
                external_flow=plan.flows.get(d, 0.0),
                spy_close=plan.spy.get(d), provisional=False,
                composition={"schema": 1, "legs": plan.legs.get(d, []), "backfill": meta},
                **extra,
            ))
            written += 1
        if plan.rebase is not None:
            row, original, rebased = plan.rebase
            row.external_flow = rebased
            row.composition = {**(row.composition or {}), "backfill_rebase": {
                "by": BACKFILL_MARKER, "at": stamp, "gap": gap, "external_flow": original}}
    session.commit()
    return written


def undo(session: Session, *, start: date, end: date, dry_run: bool = True) -> int:
    """Delete the rows this module wrote in [start, end] — matched on the backfill marker,
    so a row a live refresh wrote is never touched. Returns the count (would-be on dry run)."""
    n = 0
    for model in (models.NavSnapshot, models.AccountNavSnapshot):
        rows = session.scalars(
            select(model).where(model.snap_date >= start, model.snap_date <= end)
        ).all()
        for r in rows:
            if ((r.composition or {}).get("backfill") or {}).get("by") == BACKFILL_MARKER:
                n += 1
                if not dry_run:
                    session.delete(r)
    if not dry_run:
        session.commit()
    return n


def _stash_in_range(stash, start: date, end: date) -> bool:
    if not isinstance(stash, dict) or stash.get("by") != BACKFILL_MARKER:
        return False
    gap = stash.get("gap")
    if not gap:
        return False
    return date.fromisoformat(gap[0]) <= end and date.fromisoformat(gap[1]) >= start


def restore_stashes(session: Session, *, start: date, end: date, dry_run: bool = True) -> int:
    """Undo's other half: put back every anchor this module restated and every B flow it
    rebased, for runs whose gap overlaps [start, end]. Returns the count of stashes
    (would-be on dry run). The anchors and B sit outside the gap, so the scan starts at the
    plan's own lookback and has no upper bound."""
    n = 0
    for model in (models.NavSnapshot, models.AccountNavSnapshot):
        rows = session.scalars(
            select(model).where(model.snap_date >= start - timedelta(days=_HISTORY_LOOKBACK_DAYS))
        ).all()
        for r in rows:
            comp = dict(r.composition or {})
            restate, rebase = comp.get("backfill_restate"), comp.get("backfill_rebase")
            hit = False
            if _stash_in_range(restate, start, end):
                n, hit = n + 1, True
                if not dry_run:
                    comp.pop("backfill_restate")
                    comp["legs"] = restate["legs"]
                    r.nav = restate["nav"]
                    r.provisional = restate["provisional"]
            if _stash_in_range(rebase, start, end):
                n, hit = n + 1, True
                if not dry_run:
                    comp.pop("backfill_rebase")
                    r.external_flow = rebase["external_flow"]
            if hit and not dry_run:
                r.composition = comp
    if not dry_run:
        session.commit()
    return n
