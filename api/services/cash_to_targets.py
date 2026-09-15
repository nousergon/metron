"""New cash to my targets (metron-ops-I311) — "the user types the number; Metron does
the arithmetic" (intelligence-doctrine layer 2 exemption; positioning metron.md §3c.2).

The L1 comparable to ``deploy_cash``: that module RANKS candidates by a Metron-computed
technical score. This module chooses no names and applies no score — every candidate is a
security the user listed in ``plan_targets.targets``, in the order the user listed it, and
the only computation is arithmetic: whole-share purchases that minimize the total absolute
deviation of post-purchase weights from the user's own targets, under the user's own
position cap and minimum line size.

## Why greedy-in-target-order is the OPTIMAL allocation, not just a convenient one

The objective is Σ|weight_after_i − target_i| over a FIXED denominator (today's priced
market value plus the whole amount being deployed — see ``deploy_cash._denominator`` for
the identical fixed-basis reasoning). Buying a candidate PAST its own target strictly
INCREASES its deviation, so an optimal plan never does that — each candidate's purchase is
capped at exactly the dollar gap to its own target (and to the user's position cap, if
tighter). Below that cap, every dollar spent on any under-target candidate reduces the
total objective by exactly the same amount (1 / basis), regardless of WHICH under-target
candidate receives it — so when the amount runs out before every gap is filled, any split
of the remaining dollars across still-open gaps is equally optimal. That degeneracy is
resolved by spending in the user's own target-list order (never by any score), which is
also what the issue's acceptance criteria require ("rows ordered by the user's target
list").

Every input is the user's own Metron DB — no vendor fetch, no candidate widening. A
candidate never rated, never scored, never chosen: the eligible set is exactly
``config.targets``, always.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from api.services import analytics
from api.services import prices as price_service

# Verbatim from metron-ops-I311 — rendered unchanged by the panel.
DISCLAIMER = "Arithmetic against the targets you set. Metron does not choose securities."


@dataclass(frozen=True)
class TargetLine:
    symbol: str
    weight: float


@dataclass(frozen=True)
class PlanTargetsConfig:
    """The user's own rules, echoed back verbatim in the plan so the UI never shows a
    limit it didn't actually run under."""

    targets: tuple[TargetLine, ...]
    max_single_position: float | None = None
    min_line_usd: float = 0.0


@dataclass
class CashToTargetsLine:
    symbol: str
    target_weight: float
    weight_before: float
    weight_after: float
    shares: int
    usd: float
    price: float
    price_as_of: date | None


@dataclass
class CashToTargetsPlan:
    as_of: date
    amount_usd: float
    allocated_usd: float
    unallocated_usd: float
    unallocated_reasons: list[str]
    lines: list[CashToTargetsLine]
    portfolio_value: float
    deployment_basis: float
    base_currency: str
    config: PlanTargetsConfig
    disclaimer: str = DISCLAIMER
    skipped: list[dict] = field(default_factory=list)


_UNALLOCATED_REASON_TEXT: dict[str, str] = {
    "at_or_above_target": "some targets are already met or above",
    "at_or_above_cap": "the position-cap limit was reached for some targets",
    "unpriced": "some targets have no cached price",
    "min_line_usd": "the remaining line for a target is below the minimum line size",
    "cash_exhausted": "the remainder is smaller than the minimum line size",
}


def build_plan(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    amount_usd: float,
    config: PlanTargetsConfig,
    *,
    as_of: date | None = None,
    price_reader=None,
) -> CashToTargetsPlan:
    """Greedy-fill-to-target in the user's own target-list order (see module docstring
    for why this order is optimal, not just deterministic). ``price_reader`` is an
    injectable test seam, mirroring ``deploy_cash.build_candidates``."""
    as_of = as_of or date.today()
    amount_usd = max(0.0, float(amount_usd))

    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    base_currency = analytics._base_currency(session, portfolio_id)
    portfolio_value = sum(h.market_value for h in held if h.market_value is not None)
    held_by_ticker = {h.ticker: h for h in held if h.ticker}
    basis = portfolio_value + amount_usd

    skipped: list[dict] = []
    if not config.targets:
        return CashToTargetsPlan(
            as_of=as_of, amount_usd=round(amount_usd, 2), allocated_usd=0.0,
            unallocated_usd=round(amount_usd, 2), unallocated_reasons=[],
            lines=[], portfolio_value=round(portfolio_value, 2), deployment_basis=round(basis, 2),
            base_currency=base_currency, config=config, skipped=skipped,
        )

    symbols = [t.symbol for t in config.targets]
    reader = price_reader if price_reader is not None else price_service.latest_close_by_symbol
    ccy_by_symbol = dict.fromkeys(symbols, base_currency)
    priced = reader(session, symbols, currency_by_symbol=ccy_by_symbol)

    remaining = amount_usd
    lines: list[CashToTargetsLine] = []
    for t in config.targets:  # the user's own order — the eligible set is exactly this list
        existing = held_by_ticker.get(t.symbol)
        existing_mv = (existing.market_value or 0.0) if existing is not None else 0.0
        weight_before = (existing_mv / basis) if basis > 0 else 0.0

        point = priced.get(t.symbol)
        if point is None or not point.close or point.close <= 0:
            skipped.append({"symbol": t.symbol, "reason": "unpriced", "detail": "no cached close for this symbol"})
            continue

        target_usd = t.weight * basis
        cap_usd = (config.max_single_position * basis) if config.max_single_position is not None else None
        ceiling = target_usd if cap_usd is None else min(target_usd, cap_usd)
        headroom = ceiling - existing_mv
        if headroom <= 0:
            over_cap = cap_usd is not None and ceiling <= cap_usd + 1e-9 and existing_mv >= cap_usd
            reason = "at_or_above_cap" if over_cap else "at_or_above_target"
            skipped.append({"symbol": t.symbol, "reason": reason, "detail": f"already {reason.replace('_', ' ')}"})
            continue

        if remaining < config.min_line_usd:
            skipped.append({
                "symbol": t.symbol, "reason": "cash_exhausted",
                "detail": f"only {remaining:,.2f} left, below the {config.min_line_usd:,.0f} minimum line",
            })
            continue

        budget = min(remaining, headroom)
        shares = math.floor(budget / point.close)
        usd = round(shares * point.close, 2)
        if shares <= 0 or usd < config.min_line_usd:
            skipped.append({
                "symbol": t.symbol, "reason": "min_line_usd",
                "detail": f"the largest line the targets allow is {usd:,.2f}, below the {config.min_line_usd:,.0f} minimum",
            })
            continue

        weight_after = (existing_mv + usd) / basis if basis > 0 else 0.0
        lines.append(CashToTargetsLine(
            symbol=t.symbol, target_weight=t.weight, weight_before=weight_before, weight_after=weight_after,
            shares=shares, usd=usd, price=float(point.close), price_as_of=getattr(point, "bar_date", None),
        ))
        remaining = round(remaining - usd, 2)

    allocated = round(sum(line.usd for line in lines), 2)
    unallocated = round(amount_usd - allocated, 2)

    unallocated_reasons: list[str] = []
    if unallocated > 0:
        seen: set[str] = set()
        for row in skipped:
            key = str(row["reason"])
            if key in seen:
                continue
            seen.add(key)
            unallocated_reasons.append(_UNALLOCATED_REASON_TEXT.get(key, key))
        if not unallocated_reasons:
            unallocated_reasons.append(_UNALLOCATED_REASON_TEXT["min_line_usd"])

    return CashToTargetsPlan(
        as_of=as_of, amount_usd=round(amount_usd, 2), allocated_usd=allocated, unallocated_usd=unallocated,
        unallocated_reasons=unallocated_reasons, lines=lines, portfolio_value=round(portfolio_value, 2),
        deployment_basis=round(basis, 2), base_currency=base_currency, config=config, skipped=skipped,
    )
