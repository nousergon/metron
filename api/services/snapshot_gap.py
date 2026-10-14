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
"""

from __future__ import annotations

import bisect
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import fx as fx_service
from api.services import prices as price_service

logger = logging.getLogger(__name__)

BACKFILL_MARKER = "backfill-snapshot-gap"
# B's recorded NAV must be reproduced this closely by the gap pricing method, or the
# series is not trusted (fund legs and FX make an exact match unusual; 0.5% is far below
# any plausible one-day move the backfill could be mistaken for).
_SELF_CHECK_TOLERANCE = 0.005
_QTY_REL_TOLERANCE = 1e-9
_HISTORY_LOOKBACK_DAYS = 14


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

    def as_dict(self) -> dict:
        return {
            "grain": self.grain,
            "tenant_id": str(self.tenant_id),
            "portfolio_id": str(self.portfolio_id),
            "account_id": str(self.account_id) if self.account_id else None,
            "missing": [d.isoformat() for d in self.missing],
            "points": {d.isoformat(): round(v, 2) for d, v in sorted(self.points.items())},
            "skipped": self.skipped,
            "self_check": None if self.self_check is None else round(self.self_check, 6),
        }


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


def _plan_series(plan: SeriesPlan, a, b, history, fx_hist) -> None:
    """Fill ``plan.points`` for ``plan.missing`` from A/B compositions, or set ``skipped``."""
    if a is None or b is None:
        plan.skipped = "no snapshot on one side of the gap"
        return
    a_legs = (a.composition or {}).get("legs") or []
    b_legs = (b.composition or {}).get("legs") or []
    if not a_legs or not b_legs:
        plan.skipped = "no recorded composition to value from"
        return
    a_agg, b_agg = _aggregate(a_legs), _aggregate(b_legs)
    if set(a_agg) != set(b_agg):
        plan.skipped = "holdings differ across the gap"
        return
    for k, bl in b_agg.items():
        if not _same_qty(a_agg[k]["qty"], bl["qty"]):
            plan.skipped = f"quantity of {k[0]} changed inside the gap"
            return

    # A US session is a day SPY closed on. On one, every USD leg with a close history must
    # have its own close for that day; carrying an earlier one is the copy-forward this
    # module exists to avoid. Non-session days (weekends, holidays) carry as-of, which is
    # what a live weekend snapshot does too.
    us_sessions = {p.bar_date for p in history.get("SPY") or []}

    # Per-leg pricing for an arbitrary day; None means "cannot price this day honestly".
    def value_on(d: date) -> tuple[float, list[dict]] | None:
        total, legs = 0.0, []
        for (ticker, ccy), bl in b_agg.items():
            qty, value = bl["qty"], bl["value"]
            if qty == 0 or value == 0:
                continue
            price_b = bl["price"]
            fx_b = bl["fx_rate"] if bl["fx_rate"] is not None else 1.0
            if not price_b or not fx_b:
                return None
            mult = value / (qty * price_b * fx_b)
            series = history.get(ticker)
            if series:
                if series[-1].bar_date < d:
                    return None  # history stops before d: no close for that day
                bar = _asof(series, d)
                if bar is None:
                    return None
                if ccy == "USD" and d in us_sessions and bar.bar_date != d:
                    return None  # a US session with no close for this leg: never carry one
                px, px_date = bar.close, bar.bar_date
            else:
                # No close history (cash, or a broker-priced holding). Only a price that
                # is the same at both ends is known for the days between.
                if a_agg[(ticker, ccy)]["price"] != price_b:
                    return None
                px, px_date = price_b, None
            rate = fx_hist.rate_as_of(ccy, d)
            if rate is None:
                return None
            leg_value = qty * px * rate * mult
            total += leg_value
            legs.append({
                "ticker": ticker, "qty": qty, "price": px,
                "price_date": px_date.isoformat() if px_date else None,
                "currency": ccy, "fx_rate": rate, "value": leg_value,
                "is_fund": bl["is_fund"], "stale": False, "proxy": None,
            })
        return total, legs

    errors = []
    for snap in (a, b):
        got = value_on(snap.snap_date)
        if got is None:
            plan.skipped = f"cannot re-price the {snap.snap_date.isoformat()} snapshot (missing close or FX)"
            return
        nav = float(snap.nav)
        if not nav:
            plan.skipped = f"the {snap.snap_date.isoformat()} snapshot has zero NAV"
            return
        errors.append(abs(got[0] - nav) / nav)
    plan.self_check = max(errors)
    if plan.self_check > _SELF_CHECK_TOLERANCE:
        plan.skipped = "pricing method does not reproduce the bracketing snapshots' recorded NAV"
        return
    plan.cost_basis = float(a.cost_basis or 0.0)
    spy = history.get("SPY")
    for d in plan.missing:
        got = value_on(d)
        if got is None:
            plan.skipped = f"no close for a held leg on {d.isoformat()}"
            plan.points.clear()
            plan.legs.clear()
            return
        plan.points[d], plan.legs[d] = got
        bar = _asof(spy, d)
        plan.spy[d] = bar.close if bar else None


def plan_gap(
    session: Session, *, start: date, end: date, exclude_portfolios: set[uuid.UUID] = frozenset()
) -> list[SeriesPlan]:
    """Read-only: every portfolio and account series with a missing day in [start, end],
    each with its proposed NAV per missing day or the reason it is skipped."""
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
            plans.append((plan, a, b))

    tickers = sorted({"SPY"} | {
        leg["ticker"]
        for _p, a, b in plans
        for snap in (a, b) if snap is not None and snap.composition
        for leg in snap.composition.get("legs") or []
    })
    ccys = sorted({
        (leg.get("currency") or "USD").upper()
        for _p, a, b in plans
        for snap in (a, b) if snap is not None and snap.composition
        for leg in snap.composition.get("legs") or []
    })
    last_b = max((b.snap_date for _p, _a, b in plans if b is not None), default=end)
    history = price_service.close_history_by_symbol(
        session, tickers, start_date=start - timedelta(days=_HISTORY_LOOKBACK_DAYS), end_date=last_b
    ) if tickers else {}
    fx_hist = fx_service.rate_history(session, ccys)
    for plan, a, b in plans:
        _plan_series(plan, a, b, history, fx_hist)
    return [p for p, _a, _b in plans]


def apply_plan(session: Session, plans: list[SeriesPlan]) -> int:
    """Insert one row per planned (series, day). Never overwrites an existing row. Commits."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    written = 0
    for plan in plans:
        if plan.skipped or not plan.points:
            continue
        model = models.NavSnapshot if plan.grain == "portfolio" else models.AccountNavSnapshot
        owner = (model.portfolio_id == plan.portfolio_id) if plan.grain == "portfolio" \
            else (model.account_id == plan.account_id)
        extra = {"account_id": plan.account_id} if plan.grain == "account" else {}
        for d, nav in sorted(plan.points.items()):
            exists = session.scalars(
                select(model.id).where(model.tenant_id == plan.tenant_id, owner, model.snap_date == d)
            ).first()
            if exists is not None:
                continue  # a live writer filled it since the plan was made
            session.add(model(
                tenant_id=plan.tenant_id, portfolio_id=plan.portfolio_id, snap_date=d,
                nav=nav, cost_basis=plan.cost_basis, external_flow=0.0,
                spy_close=plan.spy.get(d), provisional=False,
                composition={
                    "schema": 1, "legs": plan.legs.get(d, []),
                    "backfill": {"by": BACKFILL_MARKER, "at": stamp},
                },
                **extra,
            ))
            written += 1
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
