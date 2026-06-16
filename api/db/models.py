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
    # User-set display label (Settings). Distinct from broker ``name`` so a re-sync never
    # clobbers it; persistence never writes this column. NULL = fall back to name/external_id.
    nickname: Mapped[str | None] = mapped_column(String(200), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Tagging — persisted from the connector snapshot (was discarded pre-multicurrency).
    institution: Mapped[str | None] = mapped_column(String(120), nullable=True)  # "Fidelity", "Interactive Brokers"
    account_type: Mapped[str | None] = mapped_column(String(60), nullable=True)  # "IRA", "Roth IRA", "Brokerage", …
    tax_treatment: Mapped[str | None] = mapped_column(String(20), nullable=True)  # taxable | tax_deferred | tax_exempt
    # Manual taxable override (Settings). NULL = auto-derive from tax_treatment/account_type.
    taxable_override: Mapped[bool | None] = mapped_column(nullable=True)
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
    exchange: Mapped[str | None] = mapped_column(String(20), nullable=True)  # broker listing exchange (e.g. SEHK)
    # Symbol yfinance prices this under (foreign listings need an exchange suffix, e.g.
    # 1299 → 1299.HK). Resolved at ingestion via prices.symbology; overridable from Settings.
    yf_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # No public listing any market-data provider can price — e.g. a 401(k) plan-level
    # CIT (PCKM, the TRP Retirement Blend trust): the broker snapshot is the price
    # authority. Excluded from the published holdings universe so the data spine never
    # asks yfinance for it (config#1029 Flow Doctor storm). Nullable so the column
    # auto-ALTERs onto an existing SQLite DB; NULL/False both mean "listed".
    yf_unlisted: Mapped[bool | None] = mapped_column(nullable=True, default=None)
    asset_class: Mapped[str | None] = mapped_column(String(40), nullable=True)  # equity | etf | cash | …
    sector: Mapped[str | None] = mapped_column(String(60), nullable=True)  # canonical GICS label; resolved lazily
    next_earnings_date: Mapped[date | None] = mapped_column(nullable=True)  # cached next earnings; refreshed on demand


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
    # Broker-reported native price / market value (IBKR Flex markPrice / positionValue).
    # The valuation fallback when yfinance can't resolve a foreign listing. NULL = none reported.
    market_price: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    market_value_local: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
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


class NavSnapshot(Base):
    """A dated portfolio NAV snapshot — the forward-recorded performance series.

    Like robodashboard's snapshot model: market value can't be reconstructed for the
    past from cost basis alone, so NAV history accumulates one day at a time as the
    user refreshes prices (idempotent per day). ``external_flow`` is that day's net
    deposit/withdrawal (portfolio perspective: + in, − out) so time-weighted return can
    neutralize cash-flow timing; ``spy_close`` is captured for the eventual NAV-vs-SPY
    benchmark. Tenant-scoped."""

    __tablename__ = "nav_snapshots"
    __table_args__ = (UniqueConstraint("tenant_id", "portfolio_id", "snap_date", name="uq_navsnapshot_day"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    snap_date: Mapped[date] = mapped_column(index=True)
    nav: Mapped[float] = mapped_column(Numeric(28, 10))
    cost_basis: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    external_flow: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    spy_close: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AccountNavSnapshot(Base):
    """A dated per-ACCOUNT NAV snapshot — the account-grain sibling of ``NavSnapshot``.

    Forward-recorded daily (additive; it does NOT replace the portfolio snapshot). Per-account
    NAV history can't be reconstructed for snapshot-sourced accounts (IBKR Flex / SnapTrade
    report current positions, not a per-account activity feed), so account-level performance
    can only accrue going forward — this table starts that accrual. Summing the rows for a
    selected set of accounts on a given day gives that subset's NAV. Tenant-scoped."""

    __tablename__ = "account_nav_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "snap_date", name="uq_account_navsnapshot_day"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    snap_date: Mapped[date] = mapped_column(index=True)
    nav: Mapped[float] = mapped_column(Numeric(28, 10))
    cost_basis: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    external_flow: Mapped[float] = mapped_column(Numeric(28, 10), default=0)
    spy_close: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FxRate(Base):
    """Global FX rate cache — ``rate`` is USD (the canonical base) per 1 unit of
    ``currency`` for ``rate_date`` (e.g. HKD → 0.128). NOT tenant-scoped: one fetch of
    ``HKDUSD=X`` serves every tenant, mirroring ``price_bars``. USD itself is never
    stored (its rate is the identity 1.0)."""

    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("currency", "rate_date", name="uq_fxrate_ccy_date"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    currency: Mapped[str] = mapped_column(String(3), index=True)  # ISO-4217 quote currency
    base: Mapped[str] = mapped_column(String(3), default="USD")   # rate is `base` per 1 `currency`
    rate_date: Mapped[date] = mapped_column(index=True)
    rate: Mapped[float] = mapped_column(Numeric(28, 10))


class InvestorPreferences(Base):
    """Per-portfolio investor preferences set from the public Settings page. One row
    per portfolio (tenant-scoped). All fields nullable so a portfolio without a saved
    preference simply uses defaults — the row is created lazily on first PUT."""

    __tablename__ = "investor_preferences"
    __table_args__ = (UniqueConstraint("tenant_id", "portfolio_id", name="uq_investorpref_portfolio"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    risk_tolerance: Mapped[str | None] = mapped_column(String(20), nullable=True)  # conservative | moderate | aggressive
    objective: Mapped[str | None] = mapped_column(String(20), nullable=True)  # income | growth | balanced
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Comma-separated SnapTrade authorization ids this portfolio's sync SKIPS
    # (linked = synced by default; exclusion = the opt-out for a broker sourced
    # elsewhere, e.g. IBKR via Flex). Keyed by stable connection id, never by
    # institution-name matching. (The legacy snaptrade_institutions column may
    # still exist physically in older SQLite files; it is unmapped and unread.)
    snaptrade_excluded_connections: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Comma-separated ``broker:external_id`` keys of broker accounts the user DELETED.
    # Enforced at the persist_snapshot chokepoint so no future import (SnapTrade, Flex,
    # CSV/OFX re-upload) silently resurrects a deleted account. The account-level
    # sibling of snaptrade_excluded_connections; restore via the Settings page.
    excluded_account_keys: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    # Comma-separated account UUIDs — the saved accounts-panel selection. Pages landing
    # without an explicit ``?account_id=`` apply this, so the selection survives
    # reloads/devices. NULL/empty = whole portfolio.
    selected_account_ids: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class WatchlistItem(Base):
    """A ticker the user is tracking but doesn't necessarily hold (metron-ops#42).

    Positions-optional: lets the product be useful with zero account data. In the
    no-feed beta the watchlist is read-only/illustrative — symbol + reference data
    (name / sector / next earnings, from the Security master) but NO live price (the
    licensed feed is a Pro cost). Tenant + portfolio scoped."""

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("tenant_id", "portfolio_id", "symbol", name="uq_watchlist_symbol"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)  # optional user label/thesis
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
