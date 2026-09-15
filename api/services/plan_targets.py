"""Plan-targets persistence (metron-ops-I311) — pulled out of ``api/routers/planning.py``
so the same create-or-replace upsert is reusable by the demo household's idempotent
reconcile (metron-ops-I322) without a service importing a router module. No behavior
change from what ``planning.py`` did inline; ``PlanTargetsIn`` validation (weight sum,
duplicate symbols) still happens at the API boundary before this is called."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models


def get_plan_targets(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> models.PlanTarget | None:
    """The portfolio's saved plan-targets row, or None if nothing has been saved yet."""
    return session.scalars(
        select(models.PlanTarget).where(
            models.PlanTarget.tenant_id == tenant_id,
            models.PlanTarget.portfolio_id == portfolio_id,
        )
    ).first()


def set_plan_targets(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    targets: list[dict],
    max_single_position: float | None,
    min_line_usd: float | None,
) -> models.PlanTarget:
    """Create or replace the portfolio's plan targets (one row per portfolio, full
    replace) — verbatim, whatever the caller passes. ``targets`` is already-validated
    ``[{"symbol": ..., "weight": ...}, ...]``."""
    row = get_plan_targets(session, tenant_id, portfolio_id)
    if row is None:
        row = models.PlanTarget(tenant_id=tenant_id, portfolio_id=portfolio_id)
        session.add(row)
    row.targets = targets
    row.max_single_position = max_single_position
    row.min_line_usd = min_line_usd
    session.commit()
    session.refresh(row)
    return row
