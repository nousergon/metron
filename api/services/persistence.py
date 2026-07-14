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
from portfolio_analytics.ingestion.schema import CanonicalActivity, activity_key, lot_key
from portfolio_analytics.prices import to_yf_symbol


@dataclass
class PersistResult:
    """What one persist call changed — surfaced in the import endpoint response."""

    accounts_created: int = 0
    securities_created: int = 0
    transactions_inserted: int = 0
    transactions_skipped: int = 0  # already present (idempotent re-import)
    positions_imported: int = 0    # broker-reported holdings written (snapshot sources)
    accounts_excluded: int = 0     # snapshot accounts skipped — user-deleted (excluded keys)
    realized_lots_inserted: int = 0  # broker closed-lot realized gains unioned (metron-ops#81)
    open_lots_imported: int = 0      # lot-level open positions written (metron-ops#74)


def account_key(broker: str, external_id: str) -> str:
    """The stable exclusion key for a broker account — ``broker:external_id``, the
    same identity the accounts upsert dedupes on (never institution-name matching)."""
    return f"{broker}:{external_id}"


def excluded_account_keys(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID
) -> set[str]:
    """The portfolio's user-deleted broker-account keys (see ``account_key``).
    Imports skip these so a deleted account stays deleted across re-syncs."""
    raw = session.scalars(
        select(models.InvestorPreferences.excluded_account_keys).where(
            models.InvestorPreferences.tenant_id == tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio_id,
        )
    ).first()
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


def snaptrade_excluded_ids(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID
) -> set[str]:
    """Authorization ids of SnapTrade connections this portfolio's sync skips (the
    rare opt-out for a broker sourced elsewhere, e.g. IBKR via Flex — syncing it from
    SnapTrade too would double-count). Shared by the interactive sync route and the
    automated ``broker_sync`` re-sync so the opt-out is honored identically either way."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio_id,
        )
    ).first()
    raw = pref.snaptrade_excluded_connections if pref is not None else None
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


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
                exchange=sec.exchange or None,
                yf_symbol=to_yf_symbol(sec.ticker, sec.currency, sec.exchange),
                asset_class=(sec.asset_type or "").lower() or None,
            )
            session.add(row)
            session.flush()  # assign PK for the activity FK below
            existing[(sec.ticker, sec.currency)] = row
            result.securities_created += 1
        else:
            # Backfill symbology on a pre-existing row (e.g. created before this feature,
            # or first seen via a source that carried no exchange). Never clobber a value
            # already set — a Settings override of yf_symbol must survive re-imports.
            if row.exchange is None and sec.exchange:
                row.exchange = sec.exchange
            if not row.yf_symbol:
                row.yf_symbol = to_yf_symbol(sec.ticker, sec.currency, sec.exchange or (row.exchange or ""))
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
        institution = acct.institution or None
        account_type = acct.account_type or None
        tax_treatment = acct.tax_treatment or None
        if row is None:
            row = models.Account(
                tenant_id=tenant_id,
                portfolio_id=portfolio_id,
                broker=broker,
                external_id=acct.number,
                name=acct.label or acct.name or None,
                currency=acct.currency or "USD",
                institution=institution,
                account_type=account_type,
                tax_treatment=tax_treatment,
            )
            session.add(row)
            session.flush()
            result.accounts_created += 1
        else:
            # Re-sync: fill blanks from the connector, but never clobber a value already
            # set (a Settings edit of institution/type/taxable must survive re-imports).
            if row.institution is None and institution:
                row.institution = institution
            if row.account_type is None and account_type:
                row.account_type = account_type
            if row.tax_treatment is None and tax_treatment:
                row.tax_treatment = tax_treatment
            # Reparent: a given broker account can only meaningfully live in one
            # portfolio's connector snapshot at a time under the app's per-portfolio
            # connector model, so a re-sync under a different portfolio must move the
            # row rather than silently leave it parented to the stale portfolio
            # (metron-ops#192).
            if row.portfolio_id != portfolio_id:
                row.portfolio_id = portfolio_id
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
        # Broker-native price/value (IBKR Flex markPrice / positionValue) — the
        # valuation fallback when yfinance can't resolve a foreign listing. Derive a
        # per-share price from the native market value when quantity is non-zero.
        mv_local = h.market_value_local or None
        market_price = (mv_local / h.quantity) if (mv_local and h.quantity) else None
        session.add(
            models.Position(
                tenant_id=tenant_id,
                account_id=account.id,
                security_id=security.id,
                quantity=h.quantity,
                avg_cost=h.avg_cost,
                currency=h.currency,
                market_price=market_price,
                market_value_local=mv_local,
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
    # User-deleted accounts are dropped from the snapshot BEFORE the upsert — the one
    # chokepoint every import path (SnapTrade, Flex, CSV/OFX) flows through, so a
    # deleted account can never be silently resurrected by a later sync. Their
    # activities/positions then skip naturally (no account row to attach to).
    excluded = excluded_account_keys(session, tenant_id, portfolio_id)
    if excluded:
        kept = [a for a in snapshot.accounts if account_key(snapshot.source, a.number) not in excluded]
        result.accounts_excluded = len(snapshot.accounts) - len(kept)
        snapshot.accounts = kept
    securities = _upsert_securities(session, snapshot, result)
    accounts = _upsert_accounts(session, snapshot, tenant_id, portfolio_id, result)
    _insert_activities(session, snapshot, tenant_id, accounts, securities, result)
    _replace_positions(session, snapshot, tenant_id, accounts, securities, result)
    _replace_open_lots(session, snapshot, tenant_id, accounts, result)
    _insert_realized_lots(session, snapshot, tenant_id, accounts, result)
    session.commit()
    return result


def _replace_open_lots(
    session: Session,
    snapshot: ConnectorSnapshot,
    tenant_id: uuid.UUID,
    accounts: dict[str, models.Account],
    result: PersistResult,
) -> None:
    """Replace lot-level open positions per account (point-in-time snapshot, like
    ``_replace_positions``). Each lot carries its ``open_date`` so historical positions —
    and thus a real NAV/TWR history — can be reconstructed for snapshot-sourced accounts
    (metron-ops#74). Only accounts present in this snapshot's ``open_lots`` are touched, so
    a broker that doesn't emit lot detail leaves its lots untouched."""
    touched: set[uuid.UUID] = {
        accounts[lot.account_number].id for lot in snapshot.open_lots if lot.account_number in accounts
    }
    for account_id in touched:
        session.execute(delete(models.OpenLot).where(models.OpenLot.account_id == account_id))
    for lot in snapshot.open_lots:
        account = accounts.get(lot.account_number)
        if account is None:
            continue
        session.add(
            models.OpenLot(
                tenant_id=tenant_id,
                account_id=account.id,
                ticker=lot.ticker,
                quantity=lot.quantity,
                open_date=lot.open_date,
                cost_basis=lot.cost_basis,
                currency=lot.currency,
                source=snapshot.source,
            )
        )
        result.open_lots_imported += 1


def _insert_realized_lots(
    session: Session,
    snapshot: ConnectorSnapshot,
    tenant_id: uuid.UUID,
    accounts: dict[str, models.Account],
    result: PersistResult,
) -> None:
    """Union broker-reported closed lots (e.g. IBKR Flex ``fifoPnlRealized``) into
    ``realized_lots`` by ``lot_key`` — idempotent across re-syncs. These are the
    authoritative realized gains the realized/Tax views surface for accounts with no
    replayable trade feed (metron-ops#81). Currency isn't carried on the parsed lot
    (IBKR closed lots are the trade currency; US equities = USD) → default USD.

    The stored key extends the canonical ``lot_key`` with ``cost_basis``: IBKR emits
    DISTINCT closed lots that share account/ticker/open/close/qty/proceeds but differ in
    cost basis (two tax lots disposed together) — they collide on the bare key and would
    violate the unique constraint. A same-batch ``seen`` guard also drops any exact
    re-emission within one snapshot."""
    seen: set[str] = set()
    for number, rg in snapshot.realized_lots:
        account = accounts.get(number)
        if account is None:
            continue
        key = f"{lot_key(number, rg)}|{rg.cost_basis}"
        if key in seen:
            continue  # exact duplicate within this snapshot
        seen.add(key)
        if session.scalar(
            select(models.RealizedLot.id).where(
                models.RealizedLot.tenant_id == tenant_id, models.RealizedLot.lot_key == key
            )
        ):
            continue  # already stored — idempotent across re-syncs
        session.add(
            models.RealizedLot(
                tenant_id=tenant_id,
                account_id=account.id,
                ticker=rg.ticker,
                open_date=rg.open_date,
                close_date=rg.close_date,
                quantity=rg.quantity,
                proceeds=rg.proceeds,
                cost_basis=rg.cost_basis,
                currency="USD",
                source=snapshot.source,
                lot_key=key,
            )
        )
        result.realized_lots_inserted += 1
