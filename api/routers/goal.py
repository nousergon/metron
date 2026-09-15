"""Retirement-goal router (metron-ops-I316) — user-authored goal inputs + the six
goal facets computed against them.

Reuses ``portfolios._owned_portfolio`` for tenant resolution rather than re-deriving
auth — every other portfolio-scoped router in this file family keys off the same
dependency (metron-ops#179). No suitability inputs are collected here: every field on
``GoalIn`` is the user's own mechanical number (doctrine layer 2 exemption)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.db import models
from api.db.session import get_session
from api.routers.portfolios import _owned_portfolio
from api.services import goal as goal_service

router = APIRouter(prefix="/portfolios/{portfolio_id}/goal", tags=["goal"])


class GoalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Every field is optional and defaults to None — a PUT with a field omitted (or
    # explicitly null) clears it. No field is ever defaulted to a non-null value:
    # Metron never suggests the target, the date, the contribution, or the rate.
    target_amount_usd: float | None = None
    target_date: date | None = None
    annual_contribution_usd: float | None = None
    withdrawal_rate: float | None = None


class GoalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_amount_usd: float | None = None
    target_date: date | None = None
    annual_contribution_usd: float | None = None
    withdrawal_rate: float | None = None


@router.get("", response_model=GoalOut)
def get_goal(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.RetirementGoal | GoalOut:
    """The portfolio's saved goal — an empty (all-null) ``GoalOut`` when the user
    hasn't set one yet. NO pre-filled values: the empty state is the contract."""
    goal = goal_service.get_goal(session, portfolio.tenant_id, portfolio.id)
    return goal or GoalOut()


@router.put("", response_model=GoalOut)
def put_goal(
    body: GoalIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.RetirementGoal:
    """Create or update the portfolio's goal (one row, full replace)."""
    return goal_service.set_goal(
        session,
        portfolio.tenant_id,
        portfolio.id,
        target_amount_usd=body.target_amount_usd,
        target_date=body.target_date,
        annual_contribution_usd=body.annual_contribution_usd,
        withdrawal_rate=body.withdrawal_rate,
    )


@router.get("/facets")
def get_goal_facets(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> dict:
    """The six goal facets (progress / trajectory range / timing cost / fee drag /
    asset-location drag / withdrawal readiness), each ``{"available": bool, "as_of":
    str, ...}``. An unset goal degrades every facet to ``available: False`` with a
    ``reason`` rather than a 404 — the caller (glance screen, Overview goal card)
    renders the empty state from that, not from an error branch."""
    facets = goal_service.compute_facets(session, portfolio.tenant_id, portfolio.id)
    return {
        "goal_progress": facets.goal_progress,
        "goal_trajectory_range": facets.goal_trajectory_range,
        "goal_timing_cost": facets.goal_timing_cost,
        "goal_fee_drag": facets.goal_fee_drag,
        "goal_asset_location_drag": facets.goal_asset_location_drag,
        "goal_withdrawal_readiness": facets.goal_withdrawal_readiness,
    }
