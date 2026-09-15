"""Plan targets — "New cash to my targets" + "What-if purchase" (metron-ops-I311).

The L1 comparable to ``deploy_cash`` (metron-ops#300): deploy_cash RANKS candidates by a
Metron-computed technical score (owner/feed-entitled build only, gated hard). This surface
chooses no names and applies no score — every number here is arithmetic against a table
the user typed in (``plan_targets``), so both features ship in ``_BETA`` and are visible on
the external demo. See ``docs/intelligence-doctrine.md`` layer 2 ("user-authored targets
are a different class and are exempt from the wall") for why this is not advice.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.db.session import get_session
from api.routers.portfolios import _owned_portfolio
from api.services import cash_to_targets as cash_to_targets_service
from api.services import whatif_purchase as whatif_service

router = APIRouter(prefix="/portfolios/{portfolio_id}/plan", tags=["planning"])


def _effective_feature(feature_key: str, x_preview_tier: str | None, x_preview_feed: str | None) -> dict:
    """Mirrors ``portfolios._effective_entitlement`` (kept local rather than importing a
    leading-underscore private helper across router modules)."""
    tier = settings.default_tier
    feed = settings.feed_entitled
    if settings.tier_simulator:
        if x_preview_tier is not None:
            tier = x_preview_tier
        if x_preview_feed is not None:
            feed = x_preview_feed.strip().lower() == "true"
    try:
        resolved = ent.resolve(tier, feed_enabled=feed)
    except ValueError:
        resolved = ent.resolve(settings.default_tier, feed_enabled=settings.feed_entitled)
    return next(f for f in resolved["features"] if f["key"] == feature_key)


def _feed_entitled(x_preview_feed: str | None) -> bool:
    feed = settings.feed_entitled
    if settings.tier_simulator and x_preview_feed is not None:
        feed = x_preview_feed.strip().lower() == "true"
    return feed


# ── Schemas ────────────────────────────────────────────────────────────────────
class TargetLineIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    weight: float = Field(gt=0, le=1)


class TargetLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    weight: float


class PlanTargetsIn(BaseModel):
    targets: list[TargetLineIn] = Field(default_factory=list)
    max_single_position: float | None = Field(default=None, gt=0, le=1)
    min_line_usd: float | None = Field(default=None, ge=0)

    @field_validator("targets")
    @classmethod
    def _validate_targets(cls, v: list[TargetLineIn]) -> list[TargetLineIn]:
        symbols = [t.symbol.strip().upper() for t in v]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Each symbol may appear only once.")
        total = sum(t.weight for t in v)
        if total > 1.0 + 1e-9:
            raise ValueError(f"Target weights sum to {total:.4f}, which is over 100%.")
        return v


class PlanTargetsOut(BaseModel):
    targets: list[TargetLineOut] = Field(default_factory=list)
    max_single_position: float | None = None
    min_line_usd: float | None = None
    available: bool = True
    reason: str | None = None
    required_tier: str | None = None


class CashToTargetsLineOut(BaseModel):
    symbol: str
    target_weight: float
    weight_before: float
    weight_after: float
    shares: int
    usd: float
    price: float
    price_as_of: date | None


class CashToTargetsIn(BaseModel):
    amount_usd: float = Field(gt=0)


class CashToTargetsPlanOut(BaseModel):
    as_of: date
    amount_usd: float
    allocated_usd: float
    unallocated_usd: float
    unallocated_reasons: list[str]
    lines: list[CashToTargetsLineOut]
    portfolio_value: float
    deployment_basis: float
    base_currency: str
    max_single_position: float | None
    min_line_usd: float
    disclaimer: str
    available: bool = True
    reason: str | None = None
    required_tier: str | None = None


class WhatIfIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    amount_usd: float | None = Field(default=None, gt=0)
    shares: float | None = Field(default=None, gt=0)
    user_price: float | None = Field(default=None, gt=0)
    account_label: str | None = None


class ConcentrationOut(BaseModel):
    n_positions: int
    hhi: float
    effective_n: float
    top5_share: float
    top10_share: float
    max_position_ticker: str | None
    max_position_weight: float


class MixRowOut(BaseModel):
    key: str
    weight: float


class WhatIfSnapshotOut(BaseModel):
    concentration: ConcentrationOut
    sector_mix: list[MixRowOut]
    asset_class_mix: list[MixRowOut]
    account_mix: list[MixRowOut]
    dividend_yield_on_cost: float | None


class TaxLotPreviewOut(BaseModel):
    symbol: str
    shares: float
    price: float
    trade_date: date
    cost_basis: float


class WhatIfPlanOut(BaseModel):
    as_of: date
    symbol: str
    shares: float
    usd: float
    price: float
    price_source: str
    price_as_of: date | None
    before: WhatIfSnapshotOut
    after: WhatIfSnapshotOut
    beta_available: bool
    tax_lot: TaxLotPreviewOut
    portfolio_value: float
    base_currency: str
    available: bool = True
    reason: str | None = None
    required_tier: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────
def _get_row(session: Session, portfolio: models.Portfolio) -> models.PlanTarget | None:
    return session.scalars(
        select(models.PlanTarget).where(
            models.PlanTarget.tenant_id == portfolio.tenant_id,
            models.PlanTarget.portfolio_id == portfolio.id,
        )
    ).first()


def _out(row: models.PlanTarget | None) -> PlanTargetsOut:
    if row is None:
        return PlanTargetsOut()
    return PlanTargetsOut(
        targets=[TargetLineOut(**t) for t in (row.targets or [])],
        max_single_position=row.max_single_position,
        min_line_usd=row.min_line_usd,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.get("/targets", response_model=PlanTargetsOut)
def get_targets(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> PlanTargetsOut:
    """The portfolio's saved plan targets — an EMPTY list (never a default/suggested
    value) when nothing has been saved yet (intelligence-doctrine layer 2)."""
    feat = _effective_feature("cash_to_targets", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return PlanTargetsOut(available=False, reason=feat["reason"], required_tier=feat["required_tier"])
    return _out(_get_row(session, portfolio))


@router.put("/targets", response_model=PlanTargetsOut)
def put_targets(
    body: PlanTargetsIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> PlanTargetsOut:
    """Create or replace the portfolio's plan targets — the user's own list, verbatim."""
    row = _get_row(session, portfolio)
    payload = [{"symbol": t.symbol.strip().upper(), "weight": t.weight} for t in body.targets]
    if row is None:
        row = models.PlanTarget(tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id)
        session.add(row)
    row.targets = payload
    row.max_single_position = body.max_single_position
    row.min_line_usd = body.min_line_usd
    session.commit()
    session.refresh(row)
    return _out(row)


@router.post("/cash-to-targets", response_model=CashToTargetsPlanOut)
def post_cash_to_targets(
    body: CashToTargetsIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> CashToTargetsPlanOut:
    feat = _effective_feature("cash_to_targets", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return CashToTargetsPlanOut(
            as_of=date.today(), amount_usd=body.amount_usd, allocated_usd=0.0, unallocated_usd=body.amount_usd,
            unallocated_reasons=[], lines=[], portfolio_value=0.0, deployment_basis=0.0, base_currency="USD",
            max_single_position=None, min_line_usd=0.0, disclaimer=cash_to_targets_service.DISCLAIMER,
            available=False, reason=feat["reason"], required_tier=feat["required_tier"],
        )
    row = _get_row(session, portfolio)
    if row is None or not row.targets:
        raise HTTPException(status_code=422, detail="No plan targets set yet — set targets first.")
    config = cash_to_targets_service.PlanTargetsConfig(
        targets=tuple(
            cash_to_targets_service.TargetLine(symbol=t["symbol"], weight=t["weight"]) for t in row.targets
        ),
        max_single_position=row.max_single_position,
        min_line_usd=row.min_line_usd or 0.0,
    )
    plan = cash_to_targets_service.build_plan(session, portfolio.tenant_id, portfolio.id, body.amount_usd, config)
    return CashToTargetsPlanOut(
        as_of=plan.as_of, amount_usd=plan.amount_usd, allocated_usd=plan.allocated_usd,
        unallocated_usd=plan.unallocated_usd, unallocated_reasons=plan.unallocated_reasons,
        lines=[CashToTargetsLineOut(**vars(line)) for line in plan.lines],
        portfolio_value=plan.portfolio_value, deployment_basis=plan.deployment_basis,
        base_currency=plan.base_currency, max_single_position=config.max_single_position,
        min_line_usd=config.min_line_usd, disclaimer=plan.disclaimer,
    )


@router.post("/whatif", response_model=WhatIfPlanOut)
def post_whatif(
    body: WhatIfIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> WhatIfPlanOut:
    feat = _effective_feature("whatif_purchase", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        raise HTTPException(status_code=404, detail="This view isn't available on your plan.")
    feed_entitled = _feed_entitled(x_preview_feed)
    try:
        plan = whatif_service.build_whatif(
            session, portfolio.tenant_id, portfolio.id, body.symbol,
            amount_usd=body.amount_usd, shares=body.shares, user_price=body.user_price,
            account_label=body.account_label, feed_entitled=feed_entitled,
        )
    except whatif_service.WhatIfPurchaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    def _snap(s: whatif_service.WhatIfSnapshot) -> WhatIfSnapshotOut:
        return WhatIfSnapshotOut(
            concentration=ConcentrationOut(**vars(s.concentration)),
            sector_mix=[MixRowOut(**vars(r)) for r in s.sector_mix],
            asset_class_mix=[MixRowOut(**vars(r)) for r in s.asset_class_mix],
            account_mix=[MixRowOut(**vars(r)) for r in s.account_mix],
            dividend_yield_on_cost=s.dividend_yield_on_cost,
        )

    return WhatIfPlanOut(
        as_of=plan.as_of, symbol=plan.symbol, shares=plan.shares, usd=plan.usd, price=plan.price,
        price_source=plan.price_source, price_as_of=plan.price_as_of, before=_snap(plan.before),
        after=_snap(plan.after), beta_available=plan.beta_available,
        tax_lot=TaxLotPreviewOut(**vars(plan.tax_lot)), portfolio_value=plan.portfolio_value,
        base_currency=plan.base_currency,
    )
