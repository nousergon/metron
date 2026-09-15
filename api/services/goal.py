"""Retirement-goal facets (metron-ops-I316) — arithmetic against the number the user typed.

Doctrine layer 2 exemption (``docs/intelligence-doctrine.md``; positioning §3c.2): a
user-authored goal — target amount, target date, planned annual contribution, planned
withdrawal rate — is not a suitability input. Evaluating the portfolio against it is
arithmetic, not advice. **Metron never suggests the target, the date, or the
withdrawal rate** — ``RetirementGoal`` starts all-NULL and every value here traces to
a number the user typed into ``PUT /portfolios/{id}/goal``.

Six facets, each deterministic and as-of stamped (positioning §3d rows in parens):

- ``progress`` — value ÷ target, and its change over the trailing year.
- ``trajectory_range`` — years-to-goal as a RANGE from the portfolio's own trailing
  1y / 3y / since-inception TWR plus the user's contribution. **Never a forecast**: no
  capital-market assumptions, no Monte Carlo — the only rate inputs are TWRs this
  module already measured from recorded NAV history.
- ``timing_cost_years`` (K1) — the MWR−TWR gap expressed as years added to the
  trajectory.
- ``fee_drag`` (B10) — blended fund expense in $/yr and years added. **Always
  unavailable**: no expense-ratio source (``feed``/``etf_vendor``) is wired into this
  codebase yet, and the gotcha is explicit that a partial number is worse than none.
- ``asset_location_drag`` (A10) — trailing-12mo dividend/interest income split
  taxable vs sheltered. No tax-rate input exists yet (not part of this issue's goal
  inputs), so this shows **income dollars only** — never an invented personal rate.
- ``withdrawal_readiness`` — months of the user's own withdrawal rate that income +
  cash covers, per account tax-treatment bucket.

Trajectory/timing-cost arithmetic never extrapolates past ``_CAP_YEARS``: beyond that
the facet reports "not reachable in the modeled horizon" rather than a wild number.

``GET /portfolios/{id}/goal/facets`` renders all six via ``compute_facets``. The
``goal_observation_<facet_key>`` functions + the ``GOAL_OBSERVATIONS`` registry near
the bottom of this module are a SEPARATE, narrower contract for the glance screen
(``api.services.glance``, metron-PR460 — not merged as of this file, so not importable
here): each returns plain observation dicts a glance producer wraps, rather than a
typed ``Candidate`` this module can't yet reference. See this module's PR body under
"Follow-up" for the wiring step once both land.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import account_meta, performance

# Simulation horizon for the years-to-goal search — beyond this the facet reports
# "not reachable" rather than a number nobody should plan around.
_CAP_YEARS = 100

# Trailing window for the dividend/interest income used by asset-location drag and
# withdrawal readiness. A calendar year (not "YTD") so the figure doesn't shrink
# every January and is comparable regardless of when the user looks.
_INCOME_TRAILING_DAYS = 365

_DIVIDEND_INTEREST_TYPES = ("DIVIDEND", "INTEREST")


# ── goal inputs (CRUD) ────────────────────────────────────────────────────────


def get_goal(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> models.RetirementGoal | None:
    """The portfolio's saved goal, or None when the user hasn't set one."""
    return session.scalars(
        select(models.RetirementGoal).where(
            models.RetirementGoal.tenant_id == tenant_id,
            models.RetirementGoal.portfolio_id == portfolio_id,
        )
    ).first()


def set_goal(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    target_amount_usd: float | None,
    target_date: date | None,
    annual_contribution_usd: float | None,
    withdrawal_rate: float | None,
) -> models.RetirementGoal:
    """Create or update the portfolio's goal (one row per portfolio, full replace —
    a client that loaded the current goal and submits the whole object governs it,
    matching ``PUT /portfolios/{id}/preferences``'s convention). No field is ever
    defaulted here: a caller that omits a value stores NULL, not a suggestion."""
    goal = get_goal(session, tenant_id, portfolio_id)
    if goal is None:
        goal = models.RetirementGoal(tenant_id=tenant_id, portfolio_id=portfolio_id)
        session.add(goal)
    goal.target_amount_usd = target_amount_usd
    goal.target_date = target_date
    goal.annual_contribution_usd = annual_contribution_usd
    goal.withdrawal_rate = withdrawal_rate
    goal.updated_at = datetime.now(UTC)
    session.commit()
    session.refresh(goal)
    return goal


# ── shared arithmetic ─────────────────────────────────────────────────────────


def _years_to_target(
    current: float, target: float, annual_contribution: float, annual_rate: float | None
) -> float | None:
    """Months-then-years to close the gap from ``current`` to ``target``, compounding
    ``annual_rate`` monthly with an even monthly contribution. Deterministic
    simulation, not a closed-form annuity solve, so it handles a negative rate and a
    negative-to-positive crossing the same way. ``annual_rate`` must be a RATE THE
    CALLER ALREADY MEASURED (a trailing TWR) — this function never assumes one.

    Returns ``None`` when the rate is unknown, mathematically degenerate
    (``<= -100%``), or the target isn't reached within ``_CAP_YEARS``."""
    if current >= target:
        return 0.0
    if annual_rate is None or annual_rate <= -1.0:
        return None
    monthly_rate = (1.0 + annual_rate) ** (1.0 / 12.0) - 1.0
    monthly_contribution = annual_contribution / 12.0
    value = current
    for month in range(1, _CAP_YEARS * 12 + 1):
        value = value * (1.0 + monthly_rate) + monthly_contribution
        if value >= target:
            return round(month / 12.0, 2)
    return None


def _trailing_annualized_twr(points: list[performance.PerfPoint], *, years: int) -> float | None:
    """Annualized TWR over the trailing ``years`` ending at the last snapshot —
    reuses ``performance``'s own flow-neutralized window TWR and annualization floor
    rather than re-deriving either (metron-ops-I204 binding constraint: one flow
    definition). None below the annualization floor, matching the headline metric."""
    if len(points) < 2:
        return None
    end = points[-1].snap_date
    try:
        cutoff = end.replace(year=end.year - years)
    except ValueError:  # Feb 29 with no matching day `years` back
        cutoff = end.replace(month=2, day=28, year=end.year - years)
    window = [p for p in points if p.snap_date >= cutoff]
    if len(window) < 2:
        return None
    twr = performance._window_twr(window)
    if twr is None:
        return None
    days = (window[-1].snap_date - window[0].snap_date).days
    if days < performance._MIN_ANNUALIZE_DAYS:
        return None
    return performance.annualize(twr, days)


def _value_n_days_ago(points: list[performance.PerfPoint], days: int) -> float | None:
    """The NAV at the last snapshot at or before ``days`` ago — the "prior value" a
    period-change needs. None when the series doesn't reach back that far."""
    if not points:
        return None
    cutoff = points[-1].snap_date - timedelta(days=days)
    candidates = [p for p in points if p.snap_date <= cutoff]
    return candidates[-1].nav if candidates else None


def _account_ids(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[models.Account]:
    return list(
        session.scalars(
            select(models.Account).where(
                models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
            )
        ).all()
    )


def _dividend_interest_income_by_account(
    session: Session, tenant_id: uuid.UUID, account_ids: list[uuid.UUID], *, trailing_days: int
) -> dict[uuid.UUID, float]:
    """Sum of DIVIDEND + INTEREST transaction amounts per account over the trailing
    window — the same ledger the Income page reads, not a second income model."""
    if not account_ids:
        return {}
    cutoff = date.today() - timedelta(days=trailing_days)
    rows = session.scalars(
        select(models.Transaction).where(
            models.Transaction.tenant_id == tenant_id,
            models.Transaction.account_id.in_(account_ids),
            models.Transaction.txn_type.in_(_DIVIDEND_INTEREST_TYPES),
            models.Transaction.trade_date >= cutoff,
        )
    ).all()
    out: dict[uuid.UUID, float] = defaultdict(float)
    for r in rows:
        out[r.account_id] += float(r.amount)
    return out


# ── facet payloads ────────────────────────────────────────────────────────────


def progress(goal: models.RetirementGoal | None, current_value: float | None, points: list[performance.PerfPoint]) -> dict:
    """Progress: value ÷ target, and the change in that ratio over the trailing year."""
    as_of = (points[-1].snap_date.isoformat() if points else date.today().isoformat())
    if goal is None or not goal.target_amount_usd or float(goal.target_amount_usd) <= 0:
        return {"available": False, "as_of": as_of, "reason": "no_goal_set"}
    if current_value is None:
        return {"available": False, "as_of": as_of, "reason": "no_valuation"}
    target = float(goal.target_amount_usd)
    ratio = current_value / target
    prior_value = _value_n_days_ago(points, 365)
    period_change_ratio = (ratio - prior_value / target) if prior_value else None
    return {
        "available": True,
        "as_of": as_of,
        "current_value_usd": round(current_value, 2),
        "target_amount_usd": target,
        "progress_ratio": round(ratio, 4),
        "period_change_ratio": round(period_change_ratio, 4) if period_change_ratio is not None else None,
    }


def trajectory_range(
    goal: models.RetirementGoal | None,
    current_value: float | None,
    perf: performance.PerformanceSummary,
) -> dict:
    """Years-to-goal as a range across the trailing 1y / 3y / since-inception TWR."""
    as_of = (perf.last_date.isoformat() if perf.last_date else date.today().isoformat())
    if goal is None or not goal.target_amount_usd or float(goal.target_amount_usd) <= 0:
        return {"available": False, "as_of": as_of, "reason": "no_goal_set"}
    if current_value is None:
        return {"available": False, "as_of": as_of, "reason": "no_valuation"}
    target = float(goal.target_amount_usd)
    contribution = float(goal.annual_contribution_usd or 0.0)
    rates: dict[str, float | None] = {
        "1y": _trailing_annualized_twr(perf.points, years=1),
        "3y": _trailing_annualized_twr(perf.points, years=3),
        "since_inception": perf.annualized_twr,
    }
    years_by_window = {k: _years_to_target(current_value, target, contribution, r) for k, r in rates.items()}
    valid = [v for v in years_by_window.values() if v is not None]
    if not valid:
        return {
            "available": False,
            "as_of": as_of,
            "reason": "insufficient_history_or_unreachable",
            "rates": rates,
        }
    return {
        "available": True,
        "as_of": as_of,
        "years_low": min(valid),
        "years_high": max(valid),
        "by_window": years_by_window,
        "rates": rates,
    }


def timing_cost_years(
    goal: models.RetirementGoal | None,
    current_value: float | None,
    perf: performance.PerformanceSummary,
) -> dict:
    """K1 — the MWR/TWR gap, expressed as years added to (or shaved off) the
    trajectory: the difference between years-to-goal computed at the MWR versus the
    TWR, both since inception."""
    as_of = (perf.last_date.isoformat() if perf.last_date else date.today().isoformat())
    if goal is None or not goal.target_amount_usd or float(goal.target_amount_usd) <= 0:
        return {"available": False, "as_of": as_of, "reason": "no_goal_set"}
    if current_value is None:
        return {"available": False, "as_of": as_of, "reason": "no_valuation"}
    if perf.annualized_twr is None or perf.annualized_mwr is None:
        return {"available": False, "as_of": as_of, "reason": "insufficient_history"}
    target = float(goal.target_amount_usd)
    contribution = float(goal.annual_contribution_usd or 0.0)
    years_at_twr = _years_to_target(current_value, target, contribution, perf.annualized_twr)
    years_at_mwr = _years_to_target(current_value, target, contribution, perf.annualized_mwr)
    if years_at_twr is None or years_at_mwr is None:
        return {"available": False, "as_of": as_of, "reason": "unreachable_within_horizon"}
    return {
        "available": True,
        "as_of": as_of,
        "gap_pct": round(perf.annualized_mwr - perf.annualized_twr, 6),
        "years_added": round(years_at_mwr - years_at_twr, 2),
    }


def fee_drag(as_of: str | None = None) -> dict:
    """B10 — blended fund expense ratio in $/yr and years added.

    ALWAYS unavailable: no expense-ratio source (``feed``/``etf_vendor``) is wired
    into this codebase (searched at I316 build time — no ``expense_ratio`` field
    anywhere in ``api/`` or ``portfolio_analytics/``). Per the issue's binding
    constraint — "if only feed/etf_vendor provides them, degrade to 'not available',
    never partial" — this never emits a dollar figure computed from a missing input."""
    return {
        "available": False,
        "as_of": as_of or date.today().isoformat(),
        "reason": "expense_ratio_source_not_provisioned",
    }


def asset_location_drag(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID
) -> dict:
    """A10 — trailing-12mo dividend/interest income held taxable vs sheltered.

    No tax-rate input is part of this issue's goal fields, so this reports **income
    dollars only** — never an invented personal rate (the gotcha the issue names
    explicitly). ``estimated_tax_usd_per_year`` stays None until a rate exists;
    adding one is a later, additive change (a nullable column + this function
    multiplying it in), not a redesign."""
    as_of = date.today().isoformat()
    accounts = _account_ids(session, tenant_id, portfolio_id)
    if not accounts:
        return {"available": False, "as_of": as_of, "reason": "no_accounts"}
    taxable_ids = {a.id for a in accounts if account_meta.is_taxable(a)}
    income = _dividend_interest_income_by_account(
        session, tenant_id, [a.id for a in accounts], trailing_days=_INCOME_TRAILING_DAYS
    )
    taxable_income = sum(v for acct_id, v in income.items() if acct_id in taxable_ids)
    sheltered_income = sum(v for acct_id, v in income.items() if acct_id not in taxable_ids)
    return {
        "available": True,
        "as_of": as_of,
        "trailing_days": _INCOME_TRAILING_DAYS,
        "taxable_dividend_interest_income_usd": round(taxable_income, 2),
        "sheltered_dividend_interest_income_usd": round(sheltered_income, 2),
        "assumed_tax_rate": None,
        "estimated_tax_usd_per_year": None,
        "note": "income dollars only — no tax rate is set; Metron never assumes one",
    }


def withdrawal_readiness(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    goal: models.RetirementGoal | None,
    current_value: float | None,
) -> dict:
    """Months of income + cash, per account tax-treatment bucket, at the user's own
    withdrawal rate applied to the current portfolio value."""
    as_of = date.today().isoformat()
    if goal is None or not goal.withdrawal_rate or float(goal.withdrawal_rate) <= 0:
        return {"available": False, "as_of": as_of, "reason": "no_withdrawal_rate_set"}
    if current_value is None or current_value <= 0:
        return {"available": False, "as_of": as_of, "reason": "no_valuation"}
    rate = float(goal.withdrawal_rate)
    annual_withdrawal = current_value * rate
    monthly_withdrawal = annual_withdrawal / 12.0
    if monthly_withdrawal <= 0:
        return {"available": False, "as_of": as_of, "reason": "zero_withdrawal_amount"}
    accounts = _account_ids(session, tenant_id, portfolio_id)
    if not accounts:
        return {"available": False, "as_of": as_of, "reason": "no_accounts"}
    income = _dividend_interest_income_by_account(
        session, tenant_id, [a.id for a in accounts], trailing_days=_INCOME_TRAILING_DAYS
    )
    by_type: dict[str, dict] = {}
    for a in accounts:
        if account_meta.is_taxable(a):
            kind = "taxable"
        elif account_meta.is_tax_deferred(a):
            kind = "tax_deferred"
        else:
            kind = "tax_exempt"
        available = float(a.cash_balance_usd or 0.0) + income.get(a.id, 0.0)
        bucket = by_type.setdefault(kind, {"available_usd": 0.0})
        bucket["available_usd"] += available
    for bucket in by_type.values():
        bucket["available_usd"] = round(bucket["available_usd"], 2)
        bucket["months_covered"] = round(bucket["available_usd"] / monthly_withdrawal, 1)
    return {
        "available": True,
        "as_of": as_of,
        "withdrawal_rate": rate,
        "annual_withdrawal_usd": round(annual_withdrawal, 2),
        "monthly_withdrawal_usd": round(monthly_withdrawal, 2),
        "by_account_type": by_type,
    }


# ── orchestration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GoalFacets:
    goal_progress: dict
    goal_trajectory_range: dict
    goal_timing_cost: dict
    goal_fee_drag: dict
    goal_asset_location_drag: dict
    goal_withdrawal_readiness: dict


def compute_facets(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> GoalFacets:
    """All six goal facets for one portfolio — the payload ``GET
    /portfolios/{id}/goal/facets`` returns and the source the glance screen / Overview
    goal card render from. One ``performance()`` call feeds progress/trajectory/timing
    cost so the three agree with each other and with the Performance page."""
    goal = get_goal(session, tenant_id, portfolio_id)
    perf = performance.performance(session, tenant_id, portfolio_id)
    current_value = perf.latest_nav
    return GoalFacets(
        goal_progress=progress(goal, current_value, perf.points),
        goal_trajectory_range=trajectory_range(goal, current_value, perf),
        goal_timing_cost=timing_cost_years(goal, current_value, perf),
        goal_fee_drag=fee_drag(as_of=perf.last_date.isoformat() if perf.last_date else None),
        goal_asset_location_drag=asset_location_drag(session, tenant_id, portfolio_id),
        goal_withdrawal_readiness=withdrawal_readiness(session, tenant_id, portfolio_id, goal, current_value),
    )


# Deterministic phrasing per facet — pure arithmetic sentences over the payload's own
# fields (doctrine layer 1: every number traces to this turn's computation, no LLM).
_TEXT: dict[str, Callable[[dict], str]] = {
    "goal_progress": lambda p: f"You're {p['progress_ratio'] * 100:.0f}% of the way to your goal.",
    "goal_trajectory_range": lambda p: f"At your own returns, {p['years_low']:.0f}–{p['years_high']:.0f} years to your goal.",
    "goal_timing_cost": lambda p: f"Your timing has added {abs(p['years_added']):.1f} year(s) to the goal."
    if p["years_added"] >= 0
    else f"Your timing has saved {abs(p['years_added']):.1f} year(s) off the goal.",
    "goal_asset_location_drag": lambda p: f"${p['taxable_dividend_interest_income_usd']:.0f}/yr of dividend/interest income sits in taxable accounts.",
    "goal_withdrawal_readiness": lambda p: "Withdrawal readiness by account type is available.",
}

# Materiality heuristics per facet — normalised 0..1, producer-supplied. Deliberately
# conservative (no deviation signal yet — that needs a shown-history baseline this
# issue doesn't build); a future pass can sharpen these without changing the producer
# contract below.
_SCORE: dict[str, dict[str, float]] = {
    "goal_progress": {"materiality": 0.6},
    "goal_trajectory_range": {"materiality": 0.7},
    "goal_timing_cost": {"materiality": 0.5},
    "goal_asset_location_drag": {"materiality": 0.4},
    "goal_withdrawal_readiness": {"materiality": 0.5},
}

# Per-facet numeric value extracted from its payload — the single number a producer
# hands the ranker/glance card alongside the phrased text. `withdrawal_readiness`
# reports the MOST BINDING bucket (min months across account types), matching the
# "readiness" framing (a portfolio is only as ready as its tightest account).
_VALUE: dict[str, Callable[[dict], float | None]] = {
    "goal_progress": lambda p: p.get("progress_ratio"),
    "goal_trajectory_range": lambda p: (p["years_low"] + p["years_high"]) / 2.0,
    "goal_timing_cost": lambda p: p.get("years_added"),
    "goal_asset_location_drag": lambda p: p.get("taxable_dividend_interest_income_usd"),
    "goal_withdrawal_readiness": lambda p: (
        min(b["months_covered"] for b in p["by_account_type"].values()) if p.get("by_account_type") else None
    ),
}


def _observation(key: str, payload: dict, as_of: str) -> list[dict]:
    """One facet's payload -> zero or one plain observation dict — the producer
    contract ``api.services.glance`` (metron-PR460, not yet on this branch) reads:
    ``register_producer(facet_key)`` against a ``Producer = Callable[[GlanceContext],
    list[Candidate]]``. This module can't import that type yet (its PR hasn't merged),
    so each producer here returns plain dicts with the fields a ``Candidate`` needs
    (facet_key/text/value/materiality/as_of/surface) — the parent wires these into
    real producers once both PRs land (see PR body "Follow-up"). Empty list when the
    facet has nothing to say (unavailable, or no text template registered)."""
    if not payload.get("available"):
        return []
    text_fn = _TEXT.get(key)
    if text_fn is None:
        return []
    value_fn = _VALUE.get(key)
    return [
        {
            "facet_key": key,
            "text": text_fn(payload),
            "value": value_fn(payload) if value_fn else None,
            "materiality": _SCORE.get(key, {}).get("materiality", 0.5),
            "as_of": as_of,
            "surface": "overview",
        }
    ]


def goal_observation_goal_progress(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    goal = get_goal(session, tenant_id, portfolio_id)
    perf = performance.performance(session, tenant_id, portfolio_id)
    return _observation("goal_progress", progress(goal, perf.latest_nav, perf.points), as_of)


def goal_observation_goal_trajectory_range(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    goal = get_goal(session, tenant_id, portfolio_id)
    perf = performance.performance(session, tenant_id, portfolio_id)
    return _observation("goal_trajectory_range", trajectory_range(goal, perf.latest_nav, perf), as_of)


def goal_observation_goal_timing_cost(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    goal = get_goal(session, tenant_id, portfolio_id)
    perf = performance.performance(session, tenant_id, portfolio_id)
    return _observation("goal_timing_cost", timing_cost_years(goal, perf.latest_nav, perf), as_of)


def goal_observation_goal_fee_drag(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    """Always empty: ``fee_drag`` never carries a value (no expense-ratio source is
    provisioned), only a reason — nothing here for the ranker to weigh."""
    return []


def goal_observation_goal_asset_location_drag(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    return _observation("goal_asset_location_drag", asset_location_drag(session, tenant_id, portfolio_id), as_of)


def goal_observation_goal_withdrawal_readiness(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, as_of: str
) -> list[dict]:
    goal = get_goal(session, tenant_id, portfolio_id)
    perf = performance.performance(session, tenant_id, portfolio_id)
    payload = withdrawal_readiness(session, tenant_id, portfolio_id, goal, perf.latest_nav)
    return _observation("goal_withdrawal_readiness", payload, as_of)


# The registry ``api.services.glance`` (once its PR merges) reads to wire a producer
# per goal facet key — see this module's docstring and the PR body's "Follow-up".
GOAL_OBSERVATIONS: dict[
    str, Callable[[Session, uuid.UUID, uuid.UUID, str], list[dict]]
] = {
    "goal_progress": goal_observation_goal_progress,
    "goal_trajectory_range": goal_observation_goal_trajectory_range,
    "goal_timing_cost": goal_observation_goal_timing_cost,
    "goal_fee_drag": goal_observation_goal_fee_drag,
    "goal_asset_location_drag": goal_observation_goal_asset_location_drag,
    "goal_withdrawal_readiness": goal_observation_goal_withdrawal_readiness,
}
