"""How long each holding has been held (Brian 2026-11-04: "if we have an unrealized gain,
we should have the purchase date").

Built from the same lots the rest of Metron uses, per account so the uncombined view gets
its own figures:

- snapshot-sourced accounts (IBKR Flex / SnapTrade): the broker's open lots (``open_lots``);
- ledger accounts (CSV / OFX): the FIFO open lots replayed from their transactions.

A holding carries the date its oldest open lot was bought, the share-weighted average days
held, and how many of its shares those dates cover. Lots that cover fewer shares than the
holding (a broker feed that does not reach the opening buy) are reported as partial, never
stretched to the whole position.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics

#: (quantity, open_date) for one open lot.
Lot = tuple[float, date]


def lots_by_account(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    account_ids: Collection[uuid.UUID] | None = None,
) -> dict[tuple[uuid.UUID, str], list[Lot]]:
    """Open lots per ``(account_id, ticker)`` for the accounts in scope."""
    scope = analytics._scoped_account_ids(session, portfolio_id, None, account_ids)
    if not scope:
        return {}
    snapshot = analytics._snapshot_sourced_account_ids(session, tenant_id, portfolio_id) & scope
    out: dict[tuple[uuid.UUID, str], list[Lot]] = defaultdict(list)
    if snapshot:
        rows = session.execute(
            select(models.OpenLot.account_id, models.OpenLot.ticker, models.OpenLot.quantity, models.OpenLot.open_date)
            .where(models.OpenLot.tenant_id == tenant_id, models.OpenLot.account_id.in_(snapshot))
        ).all()
        for aid, ticker, qty, opened in rows:
            if float(qty) > 0:
                out[(aid, ticker)].append((float(qty), opened))
    ledger_ids = scope - snapshot
    if ledger_ids:
        by_account: dict[uuid.UUID, list] = defaultdict(list)
        for aid, txn in analytics.engine_transactions_by_account(
            session, tenant_id, portfolio_id, account_ids=ledger_ids
        ):
            by_account[aid].append((aid, txn))
        for aid, txns in by_account.items():
            ledger, _ = analytics.build_portfolio_ledger(txns, log=False)
            for ticker, lots in ledger.open_lots.items():
                for lot in lots:
                    if lot.quantity > 0:
                        out[(aid, ticker)].append((lot.quantity, lot.open_date))
    return dict(out)


def summarize(lots: Iterable[Lot], quantity: float, today: date) -> tuple[date | None, float | None, float | None]:
    """``(first_acquired, avg_days_held, coverage)`` for one holding of ``quantity`` shares."""
    lots = [(q, d) for q, d in lots if q > 0]
    if not lots or quantity <= 0:
        return None, None, None
    covered = sum(q for q, _ in lots)
    avg_days = sum(q * max(0, (today - d).days) for q, d in lots) / covered
    return min(d for _, d in lots), avg_days, min(1.0, covered / quantity)


def stamp(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    held: list,
    *,
    account_ids: Collection[uuid.UUID] | None = None,
    today: date | None = None,
) -> list:
    """Set ``first_acquired`` / ``avg_days_held`` / ``held_coverage`` on each holding row.
    A row with ``account_id`` (the uncombined view) uses that account's lots only."""
    today = today or date.today()
    lots = lots_by_account(session, tenant_id, portfolio_id, account_ids)
    by_ticker: dict[str, list[Lot]] = defaultdict(list)
    for (_, ticker), ls in lots.items():
        by_ticker[ticker].extend(ls)
    for h in held:
        own = lots.get((h.account_id, h.ticker), []) if h.account_id is not None else by_ticker.get(h.ticker, [])
        h.first_acquired, h.avg_days_held, h.held_coverage = summarize(own, h.quantity, today)
    return held
