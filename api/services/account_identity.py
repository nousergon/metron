"""Read-only report of CSV/manual accounts whose portfolio ownership is ambiguous
(metron-ops#383).

Until ``identity_scope`` existed, a CSV/manual account label was unique per TENANT, so
a second portfolio importing the same label (the default "CSV", say) had its activity
written into the first portfolio's account. The migration that introduced the scope
keeps every existing account exactly where it is, because no import ever recorded which
portfolio it was aimed at and any reassignment would be a guess.

This module lists the accounts that could have absorbed such imports, those in a tenant
with more than one portfolio, with enough context for a person to resolve each one. It
never writes. ``python -m api.services.account_identity`` prints the report as JSON
lines.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models


@dataclass(frozen=True)
class AmbiguousLabelAccount:
    account_id: uuid.UUID
    tenant_id: uuid.UUID
    portfolio_id: uuid.UUID
    broker: str
    portfolios_in_tenant: int
    transactions: int
    first_trade: date | None
    last_trade: date | None


def ambiguous_label_accounts(session: Session) -> list[AmbiguousLabelAccount]:
    """Every CSV/manual account in a tenant with more than one portfolio. Read-only."""
    portfolio_counts = (
        select(models.Portfolio.tenant_id, func.count(models.Portfolio.id).label("n"))
        .group_by(models.Portfolio.tenant_id)
        .subquery()
    )
    txn = (
        select(
            models.Transaction.account_id,
            func.count(models.Transaction.id).label("n"),
            func.min(models.Transaction.trade_date).label("first"),
            func.max(models.Transaction.trade_date).label("last"),
        )
        .group_by(models.Transaction.account_id)
        .subquery()
    )
    rows = session.execute(
        select(
            models.Account.id,
            models.Account.tenant_id,
            models.Account.portfolio_id,
            models.Account.broker,
            portfolio_counts.c.n,
            txn.c.n,
            txn.c.first,
            txn.c.last,
        )
        .join(portfolio_counts, portfolio_counts.c.tenant_id == models.Account.tenant_id)
        .outerjoin(txn, txn.c.account_id == models.Account.id)
        .where(models.Account.broker.in_(models.PORTFOLIO_SCOPED_SOURCES), portfolio_counts.c.n > 1)
        .order_by(models.Account.tenant_id, models.Account.id)
    ).all()
    return [
        AmbiguousLabelAccount(
            account_id=r[0],
            tenant_id=r[1],
            portfolio_id=r[2],
            broker=r[3],
            portfolios_in_tenant=r[4],
            transactions=r[5] or 0,
            first_trade=r[6],
            last_trade=r[7],
        )
        for r in rows
    ]


def main() -> int:  # pragma: no cover — thin CLI over the tested function
    from api.db.session import SessionLocal

    with SessionLocal() as session:
        for row in ambiguous_label_accounts(session):
            print(json.dumps(asdict(row), default=str, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
