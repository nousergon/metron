"""Persist a connector's canonical snapshot into the multi-tenant store.

The single bridge between the engine's source-agnostic ``ConnectorSnapshot``
(``portfolio_analytics.ingestion``) and the API's relational schema
(``api.db.models``). Every connector — CSV today, IBKR Flex / SnapTrade next — lands
through this one function, so the merge rules live in exactly one place.

Merge semantics mirror the engine's silver store (``ingestion.store``), re-expressed
per-tenant in SQL:

  * **securities** — global, identity-based **upsert** by ``(symbol, currency)``. One
    instrument master shared across all tenants (the cost-is-per-universe property).
  * **accounts** — **upsert** by ``(tenant_id, broker, external_id)`` under the target
    portfolio; a re-import updates the label, never duplicates the account.
  * **transactions** — immutable **events**, unioned by ``source_key`` so a re-upload
    of an overlapping CSV is idempotent (already-seen rows are skipped, not doubled).

Positions are deliberately NOT written here for transaction-sourced snapshots — they
are derived from the ledger at read time (``api.services.analytics``). A connector
that reports broker positions directly (Flex/SnapTrade) will populate ``positions``
through a sibling path; that is out of scope for CSV ingestion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import CanonicalActivity, activity_key


@dataclass
class PersistResult:
    """What one persist call changed — surfaced in the import endpoint response."""

    accounts_created: int = 0
    securities_created: int = 0
    transactions_inserted: int = 0
    transactions_skipped: int = 0  # already present (idempotent re-import)
    positions_imported: int = 0    # broker-reported holdings written (snapshot sources)


def _upsert_securities(
    session: Session, snapshot: ConnectorSnapshot, result: PersistResult
) -> dict[str, models.Security]:
    """Upsert the snapshot's securities into the global master, keyed by the
    canonical ``security_id`` so activities can resolve their FK."""
    wanted = {s.security_id: s for s in snapshot.securities}
    if not wanted:
        return {}
    tickers = {s.ticker for s in wanted.values()}
    existing = {
        (row.symbol, row.currency): row
        for row in session.scalars(select(models.Security).where(models.Security.symbol.in_(tickers))).all()
    }
    out: dict[str, models.Security] = {}
    for security_id, sec in wanted.items():
        row = existing.get((sec.ticker, sec.currency))
        if row is None:
            row = models.Security(
                symbol=sec.ticker,
                name=sec.name or sec.ticker,
                currency=sec.currency,
                asset_class=(sec.asset_type or "").lower() or None,
            )
            session.add(row)
            session.flush()  # assign PK for the activity FK below
            existing[(sec.ticker, sec.currency)] = row
            result.securities_created += 1
        out[security_id] = row
    return out


def _upsert_accounts(
    session: Session,
    snapshot: ConnectorSnapshot,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    result: PersistResult,
) -> dict[str, models.Account]:
    """Upsert the snapshot's accounts under the target portfolio, keyed by the
    canonical account ``number`` so activities can resolve their FK."""
    broker = snapshot.source
    out: dict[str, models.Account] = {}
    for acct in snapshot.accounts:
        row = session.scalars(
            select(models.Account).where(
                models.Account.tenant_id == tenant_id,
                models.Account.broker == broker,
                models.Account.external_id == acct.number,
            )
        ).first()
        if row is None:
            row = models.Account(
                tenant_id=tenant_id,
                portfolio_id=portfolio_id,
                broker=broker,
                external_id=acct.number,
                name=acct.label or acct.name or None,
                currency=acct.currency or "USD",
            )
            session.add(row)
            session.flush()
            result.accounts_created += 1
        out[acct.number] = row
    return out


def _insert_activities(
    session: Session,
    snapshot: ConnectorSnapshot,
    tenant_id: uuid.UUID,
    accounts: dict[str, models.Account],
    securities: dict[str, models.Security],
    result: PersistResult,
) -> None:
    """Union the snapshot's activities into ``transactions`` by ``source_key``."""
    by_account: dict[uuid.UUID, list[CanonicalActivity]] = {}
    for act in snapshot.activities:
        account = accounts.get(act.account_number)
        if account is None:  # pragma: no cover — connector emits an account per activity
            continue
        by_account.setdefault(account.id, []).append(act)

    for account_id, acts in by_account.items():
        seen = set(
            session.scalars(
                select(models.Transaction.source_key).where(models.Transaction.account_id == account_id)
            ).all()
        )
        for act in acts:
            key = activity_key(act)
            if key in seen:
                result.transactions_skipped += 1
                continue
            seen.add(key)
            security = securities.get(act.security_id) if act.security_id else None
            session.add(
                models.Transaction(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    security_id=security.id if security is not None else None,
                    txn_type=act.type.value,
                    quantity=act.quantity,
                    price=act.price,
                    fees=act.fees,
                    amount=act.amount,
                    currency=act.currency,
                    trade_date=act.when,
                    source_key=key,
                )
            )
            result.transactions_inserted += 1


def _replace_positions(
    session: Session,
    snapshot: ConnectorSnapshot,
    tenant_id: uuid.UUID,
    accounts: dict[str, models.Account],
    securities: dict[str, models.Security],
    result: PersistResult,
) -> None:
    """Replace broker-reported positions per account (point-in-time snapshot truth).

    Holdings are a snapshot, not events: each sync carries the *current* full position
    set for an account, so a closed-out position must vanish. We therefore delete the
    account's existing positions and re-insert — last-write-wins, no ghosts. (Only
    snapshot sources like Flex/SnapTrade populate ``holdings``; CSV/OFX derive
    positions from the transaction ledger instead and never reach here.)"""
    touched: set[uuid.UUID] = {
        accounts[h.account_number].id for h in snapshot.holdings if h.account_number in accounts
    }
    for account_id in touched:
        session.execute(delete(models.Position).where(models.Position.account_id == account_id))

    for h in snapshot.holdings:
        account = accounts.get(h.account_number)
        security = securities.get(h.security_id)
        if account is None or security is None:  # pragma: no cover — connector pairs holding↔security
            continue
        session.add(
            models.Position(
                tenant_id=tenant_id,
                account_id=account.id,
                security_id=security.id,
                quantity=h.quantity,
                avg_cost=h.avg_cost,
                currency=h.currency,
                as_of=(h.as_of.date() if h.as_of is not None else date.today()),
            )
        )
        result.positions_imported += 1


def persist_snapshot(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    snapshot: ConnectorSnapshot,
) -> PersistResult:
    """Persist a canonical snapshot for one tenant/portfolio and commit.

    Order matters: securities (global) → accounts (tenant) → activities + positions
    (FK both). Idempotent on re-run — securities/accounts upsert, transactions union
    by ``source_key``, positions replaced per account (snapshot semantics).
    """
    result = PersistResult()
    securities = _upsert_securities(session, snapshot, result)
    accounts = _upsert_accounts(session, snapshot, tenant_id, portfolio_id, result)
    _insert_activities(session, snapshot, tenant_id, accounts, securities, result)
    _replace_positions(session, snapshot, tenant_id, accounts, securities, result)
    session.commit()
    return result
