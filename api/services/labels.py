"""User-set security labels (metron-ops#47) — a readable alias for a symbol so an opaque
numeric CUSIP (a bond/CD) is legible. Tenant-scoped, keyed by symbol (how holdings are
addressed). Setting an empty label clears it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def labels_by_symbol(session: Session, tenant_id: uuid.UUID, symbols: list[str]) -> dict[str, str]:
    """``{symbol: label}`` for the tenant's set labels among ``symbols`` (omits unset)."""
    if not symbols:
        return {}
    rows = session.execute(
        select(models.SecurityLabel.symbol, models.SecurityLabel.label).where(
            models.SecurityLabel.tenant_id == tenant_id,
            models.SecurityLabel.symbol.in_(symbols),
        )
    ).all()
    return dict(rows)


def set_label(session: Session, tenant_id: uuid.UUID, symbol: str, label: str | None) -> str | None:
    """Set (upsert) or CLEAR (empty/None label) a symbol's label for the tenant. Returns
    the stored label, or None if cleared."""
    sym = _norm(symbol)
    if not sym:
        raise ValueError("symbol is required")
    text = (label or "").strip()
    row = session.scalars(
        select(models.SecurityLabel).where(
            models.SecurityLabel.tenant_id == tenant_id, models.SecurityLabel.symbol == sym
        )
    ).first()
    if not text:
        if row is not None:
            session.delete(row)
            session.commit()
        return None
    if row is None:
        row = models.SecurityLabel(tenant_id=tenant_id, symbol=sym, label=text)
        session.add(row)
    else:
        row.label = text
    session.commit()
    return text
