"""Portfolio analytics over persisted transactions.

Reads a portfolio's stored ``transactions`` and runs them through the engine ledger
(``portfolio_analytics.domain.ledger``) to derive **current holdings** (FIFO cost
basis) and **realized gains** (short/long-term) — the price-free analytics that a
plain transaction history fully determines.

Market value, unrealized P&L, and time-weighted performance need an EOD price series
and are intentionally out of scope here: they arrive with the price service (plan §6
PH1 Marketstack increment). Reporting a market value we cannot source would violate
the product's no-fabrication posture, so this layer returns only what the ledger
proves.

Holdings are derived live from the ledger rather than read from the ``positions``
table: for a CSV/transaction source the ledger IS the position truth, and deriving
keeps the holding reconciled to the transaction history by construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.domain.ledger import Transaction, TxnType, build_ledger


@dataclass
class Holding:
    ticker: str
    quantity: float
    avg_cost: float
    cost_basis: float


@dataclass
class RealizedLot:
    ticker: str
    open_date: date
    close_date: date
    quantity: float
    proceeds: float
    cost_basis: float
    gain: float
    long_term: bool


@dataclass
class TransactionRow:
    trade_date: date
    txn_type: str
    ticker: str
    quantity: float
    price: float
    amount: float
    fees: float
    currency: str


def _portfolio_rows(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID):
    """Fetch ``(Transaction, ticker)`` for one portfolio, tenant-scoped, oldest first."""
    stmt = (
        select(models.Transaction, models.Security.symbol)
        .join(models.Account, models.Transaction.account_id == models.Account.id)
        .outerjoin(models.Security, models.Transaction.security_id == models.Security.id)
        .where(
            models.Transaction.tenant_id == tenant_id,
            models.Account.portfolio_id == portfolio_id,
        )
        .order_by(models.Transaction.trade_date, models.Transaction.created_at)
    )
    return session.execute(stmt).all()


def _to_engine_txn(row: models.Transaction, ticker: str | None) -> Transaction:
    """Map a stored transaction to an engine ``Transaction`` (floats, not Decimal)."""
    return Transaction(
        when=row.trade_date,
        type=TxnType(row.txn_type),
        ticker=(ticker or "").upper(),
        quantity=float(row.quantity),
        price=float(row.price),
        amount=float(row.amount),
        fees=float(row.fees),
        currency=row.currency,
    )


def load_ledger(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID):
    """Build the FIFO ledger for a portfolio from its stored transactions."""
    txns = [_to_engine_txn(row, ticker) for row, ticker in _portfolio_rows(session, tenant_id, portfolio_id)]
    return build_ledger(txns)


def _position_rows(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID):
    """Fetch ``(quantity, avg_cost, ticker)`` for broker-reported positions in a
    portfolio (snapshot-sourced accounts: Flex/SnapTrade)."""
    stmt = (
        select(models.Position.quantity, models.Position.avg_cost, models.Security.symbol)
        .join(models.Account, models.Position.account_id == models.Account.id)
        .join(models.Security, models.Position.security_id == models.Security.id)
        .where(models.Position.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id)
    )
    return session.execute(stmt).all()


def holdings(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[Holding]:
    """Current open positions with share-weighted average cost + total cost basis.

    Unions the two ingestion models, aggregated by ticker: positions **derived from
    the transaction ledger** (CSV/OFX accounts) and positions **reported directly by
    the broker** (Flex/SnapTrade → the ``positions`` table). Per-account ownership
    guarantees a single account is only one source, so the two sets never double-count
    the same holding; a ticker held in both a CSV account and a Flex account correctly
    sums across accounts."""
    # ticker → [total_shares, total_cost_basis]
    agg: dict[str, list[float]] = {}

    ledger = load_ledger(session, tenant_id, portfolio_id)
    for ticker in ledger.open_lots:
        shares, avg_cost = ledger.position(ticker)
        if shares > 0:
            agg.setdefault(ticker, [0.0, 0.0])
            agg[ticker][0] += shares
            agg[ticker][1] += shares * avg_cost

    for quantity, avg_cost, ticker in _position_rows(session, tenant_id, portfolio_id):
        qty = float(quantity)
        if qty <= 0:
            continue
        agg.setdefault(ticker, [0.0, 0.0])
        agg[ticker][0] += qty
        agg[ticker][1] += qty * float(avg_cost)

    return [
        Holding(ticker=t, quantity=shares, avg_cost=basis / shares if shares else 0.0, cost_basis=basis)
        for t, (shares, basis) in sorted(agg.items())
    ]


def realized(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[RealizedLot]:
    """Closed lots with proceeds, basis, gain, and holding-period classification."""
    ledger = load_ledger(session, tenant_id, portfolio_id)
    return [
        RealizedLot(
            ticker=r.ticker,
            open_date=r.open_date,
            close_date=r.close_date,
            quantity=r.quantity,
            proceeds=r.proceeds,
            cost_basis=r.cost_basis,
            gain=r.gain,
            long_term=r.long_term,
        )
        for r in sorted(ledger.realized, key=lambda r: r.close_date)
    ]


def transactions(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[TransactionRow]:
    """The portfolio's stored transactions, oldest first."""
    return [
        TransactionRow(
            trade_date=row.trade_date,
            txn_type=row.txn_type,
            ticker=ticker or "",
            quantity=float(row.quantity),
            price=float(row.price),
            amount=float(row.amount),
            fees=float(row.fees),
            currency=row.currency,
        )
        for row, ticker in _portfolio_rows(session, tenant_id, portfolio_id)
    ]
