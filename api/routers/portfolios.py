"""Portfolios router — PH0 skeleton over the multi-tenant schema.

Tenant resolution is stubbed (``X-Tenant-Id`` header) until auth lands in PH4; the
real product derives the tenant from the authenticated session. Analytics endpoints
(performance/attribution/risk/…) are added in PH1–PH3 — this file only proves the
DB + session wiring round-trips.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.db.session import get_session

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


class PortfolioIn(BaseModel):
    name: str
    base_currency: str = "USD"


class PortfolioOut(BaseModel):
    id: uuid.UUID
    name: str
    base_currency: str


def _tenant_id(x_tenant_id: str | None = Header(default=None)) -> uuid.UUID:
    # Placeholder for the authenticated tenant (PH4 replaces this with the session).
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-Id header required (auth lands in PH4)")
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="X-Tenant-Id must be a UUID") from e


@router.get("", response_model=list[PortfolioOut])
def list_portfolios(
    tenant_id: uuid.UUID = Depends(_tenant_id),
    session: Session = Depends(get_session),
) -> list[models.Portfolio]:
    rows = session.scalars(select(models.Portfolio).where(models.Portfolio.tenant_id == tenant_id)).all()
    return list(rows)


@router.post("", response_model=PortfolioOut, status_code=201)
def create_portfolio(
    body: PortfolioIn,
    tenant_id: uuid.UUID = Depends(_tenant_id),
    session: Session = Depends(get_session),
) -> models.Portfolio:
    # Ensure the tenant row exists (idempotent for the skeleton/dev flow).
    if session.get(models.Tenant, tenant_id) is None:
        session.add(models.Tenant(id=tenant_id, name=f"tenant-{tenant_id}"))
    portfolio = models.Portfolio(tenant_id=tenant_id, name=body.name, base_currency=body.base_currency)
    session.add(portfolio)
    session.commit()
    session.refresh(portfolio)
    return portfolio
