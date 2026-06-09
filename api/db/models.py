"""Multi-tenant portfolio schema.

Every tenant-owned row carries ``tenant_id`` so production can enforce per-tenant
isolation with Postgres Row-Level Security (one policy per table:
``USING (tenant_id = current_setting('app.tenant_id')::uuid)``). Reference data that
is identical for everyone — ``securities`` and ``price_bars`` — is global and
deliberately NOT tenant-scoped, so one EOD price fetch serves all tenants (the
cost-is-per-universe-not-per-user property the plan's §3 cache note depends on).

Column semantics mirror the ``portfolio_analytics`` engine types
(``domain.ledger.TxnType``, ``Transaction``, ``Lot``) so service code can hydrate
engine objects from these rows without translation.

UUID primary keys map to native ``uuid`` on Postgres and ``CHAR(32)`` on SQLite via
``sqlalchemy.Uuid`` — the same models run on both backends unchanged.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from api.db.session import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    users: Mapped[list[User]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    portfolios: Mapped[list[Portfolio]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    tenant: Mapped[Tenant] = relationship(back_populates="portfolios")
    accounts: Mapped[list[Account]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")


class Account(Base):
    """A connected brokerage account (the FDX ``CanonicalAccount`` grain)."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("tenant_id", "broker", "external_id", name="uq_account_source"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    broker: Mapped[str] = mapped_column(String(50))         # "ibkr_flex" | "snaptrade" | "csv" | "ofx"
    external_id: Mapped[str] = mapped_column(String(120))   # broker-side account number
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    portfolio: Mapped[Portfolio] = relationship(back_populates="accounts")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="account", cascade="all, delete-orphan")
    positions: Mapped[list[Position]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Security(Base):
    """Global reference data — NOT tenant-scoped (shared across all tenants)."""

    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("symbol", "currency", name="uq_security_symbol_ccy"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    asset_class: Mapped[str | None] = mapped_column(String(40), nullable=True)  # equity | etf | cash | …


class Transaction(Base):
    """One ledger event — mirrors ``portfolio_analytics.domain.ledger.Transaction``."""

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("tenant_id", "account_id", "source_key", name="uq_txn_source_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("securities.id"), nullable=True, index=True
    )  # null for pure-cash events (deposit/withdrawal/fee)
    txn_type: Mapped[str] = mapped_column(String(20))  # TxnType.value: BUY/SELL/DIVIDEND/DEPOSIT/…
    quantity: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    price: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    fees: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    amount: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    trade_date: Mapped[date] = mapped_column(index=True)
    source_key: Mapped[str] = mapped_column(String(200))  # idempotency key from the connector (activity_key)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    account: Mapped[Account] = relationship(back_populates="transactions")


class Position(Base):
    """Current holding snapshot — mirrors the broker's reported position."""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "security_id", name="uq_position_account_security"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("securities.id"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    avg_cost: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    as_of: Mapped[date] = mapped_column(index=True)

    account: Mapped[Account] = relationship(back_populates="positions")


class PriceBar(Base):
    """Global EOD price cache — NOT tenant-scoped. One fetch serves all tenants."""

    __tablename__ = "price_bars"
    __table_args__ = (UniqueConstraint("security_id", "bar_date", name="uq_pricebar_security_date"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    security_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("securities.id", ondelete="CASCADE"), index=True)
    bar_date: Mapped[date] = mapped_column(index=True)
    close: Mapped[float] = mapped_column(Numeric(28, 10))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
