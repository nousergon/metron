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
    # Country of domicile (yfinance ``Ticker.info['country']``, Title-Case, e.g.
    # "United States"). Reference data like sector — resolved lazily from the data spine,
    # drives the holdings US-vs-international split. Nullable so the column auto-ALTERs
    # onto an existing SQLite DB; NULL = unclassified coverage gap, never guessed.
    country: Mapped[str | None] = mapped_column(String(60), nullable=True)
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


class IntradayLegSnapshot(Base):
    """A dated portfolio overnight/intraday/day P&L decomposition (metron-ops#87).

    Forward-recorded once per trading day from the intraday spine (Day = Overnight
    [open vs prior close] + Intraday [last vs open]), so the history of how much of the
    portfolio's return arrives overnight vs during the session accrues over time — it can't
    be reconstructed (the spine keeps only the latest snapshot). Skipped (never fabricated)
    when the feed is unavailable. Tenant-scoped; one row per (portfolio, snap_date)."""

    __tablename__ = "intraday_leg_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "portfolio_id", "snap_date", name="uq_intraday_leg_day"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    snap_date: Mapped[date] = mapped_column(index=True)
    # Fractions (e.g. 0.004 = +0.4%); base-currency gains; prev-close market value (the base
    # the % are over). Nullable — a leg without a usable quote is excluded, not zeroed.
    overnight_pct: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    intraday_pct: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    day_pct: Mapped[float | None] = mapped_column(Numeric(18, 10), nullable=True)
    overnight_gain: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    intraday_gain: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    day_gain: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    prev_mv: Mapped[float | None] = mapped_column(Numeric(28, 10), nullable=True)
    n_priced: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RealizedLot(Base):
    """A broker-reported closed lot — authoritative realized capital gain.

    Some brokers (IBKR) emit FIFO-computed realized P&L per closed lot (the Flex query's
    Closed Lots / ``fifoPnlRealized``) but NO replayable trade activity feed, and IBKR
    computes the gain using the FULL lot history — so a sale this year of a lot bought
    years ago carries its correct realized gain regardless of how far back the import
    window reaches. We persist these lots verbatim and surface them in the realized /
    Tax views ALONGSIDE the FIFO-from-transactions reconstruction used for activity-feed
    brokers — never both for the same account (an account with stored lots is excluded
    from the transaction replay). Idempotent on ``lot_key`` (metron-ops#81). Tenant-scoped."""

    __tablename__ = "realized_lots"
    __table_args__ = (UniqueConstraint("tenant_id", "lot_key", name="uq_realized_lot"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(index=True)
    open_date: Mapped[date]
    close_date: Mapped[date] = mapped_column(index=True)
    quantity: Mapped[float] = mapped_column(Numeric(28, 10))
    proceeds: Mapped[float] = mapped_column(Numeric(28, 10))  # native currency
    cost_basis: Mapped[float] = mapped_column(Numeric(28, 10))  # native currency
    currency: Mapped[str] = mapped_column(default="USD")
    source: Mapped[str] = mapped_column(default="")  # connector source (e.g. ibkr_flex)
    lot_key: Mapped[str] = mapped_column(index=True)  # stable identity for idempotent union
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OpenLot(Base):
    """A still-open tax lot of a holding, from broker lot-level Open Positions.

    Carries the lot's ``open_date`` so the historical position timeline — and thus a real
    NAV/TWR history — can be reconstructed for snapshot-sourced accounts (IBKR/SnapTrade)
    that have no replayable trade feed (metron-ops#74). Replaced per account each sync
    (point-in-time snapshot, like ``positions``). Tenant-scoped."""

    __tablename__ = "open_lots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(index=True)
    quantity: Mapped[float] = mapped_column(Numeric(28, 10))
    open_date: Mapped[date] = mapped_column(index=True)
    cost_basis: Mapped[float] = mapped_column(Numeric(28, 10))  # total native cost of this lot
    currency: Mapped[str] = mapped_column(default="USD")
    source: Mapped[str] = mapped_column(default="")
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
    # The SINGLE user-facing switch for the live intraday price overlay (Settings). NULL/False
    # = OFF — the default — so the persisted EOD-close valuation is authoritative and Metron
    # makes zero intraday fetches; True = overlay the ~15-min-delayed intraday last-price on
    # held positions while the app is open. Subordinate to feed entitlement (a no-feed tier
    # never offers it: overlay applies iff feed_entitled AND intraday_enabled). Nullable so the
    # column auto-ALTERs onto an existing SQLite DB; NULL reads as OFF.
    intraday_enabled: Mapped[bool | None] = mapped_column(nullable=True, default=None)
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


class SecurityLabel(Base):
    """A user-set display label/alias for a symbol (metron-ops#47).

    Bonds / CDs surface as an opaque numeric CUSIP; this lets the user attach a readable
    name so they can tell what a holding is. Tenant-scoped (keyed by symbol, matching how
    holdings are addressed) so it never leaks across tenants and a re-import never
    clobbers it. New table → auto-created on the personal SQLite (no migration)."""

    __tablename__ = "security_labels"
    __table_args__ = (UniqueConstraint("tenant_id", "symbol", name="uq_security_label_symbol"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SecurityClassification(Base):
    """A user-set GICS-sector / country-of-domicile override for a symbol.

    Sector + country are reference data sourced from the data spine (yfinance), but the
    source can't classify everything — a numeric-CUSIP bond, an illiquid foreign listing,
    a fund — leaving the holding in the honest "Unclassified" bucket of the Allocation
    breakdown. This lets the user fill (or correct) that gap so their geo/sector mix is
    complete. TENANT-SCOPED (keyed by symbol, like ``security_labels``): it overlays the
    user's own view without mutating the global ``securities`` reference row that every
    tenant shares. ``sector`` / ``country`` are independently nullable — setting one leaves
    the other untouched; clearing both deletes the row. New table → auto-created on the
    personal SQLite (no migration)."""

    __tablename__ = "security_classifications"
    __table_args__ = (UniqueConstraint("tenant_id", "symbol", name="uq_security_classification_symbol"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    sector: Mapped[str | None] = mapped_column(String(80), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class WalletAddress(Base):
    """A crypto wallet address this portfolio tracks (metron-ops#111).

    The standalone crypto page is decoupled from the broker/EOD holdings axis (crypto is
    24/7, no market close). The user manages addresses here; Metron PUBLISHES the deduped
    set to S3 (``metron/crypto/wallet_addresses.json``) and the ``nousergon-data`` producer
    reads it, queries the chain for balances, and writes ``crypto/holdings.json`` back — so
    Metron itself makes NO chain calls (the data-spine invariant). Balances join back to
    these rows by ``(chain, address)``. Tenant + portfolio scoped; new table → auto-created
    on the personal SQLite (no migration)."""

    __tablename__ = "wallet_addresses"
    __table_args__ = (
        UniqueConstraint("tenant_id", "portfolio_id", "chain", "address", name="uq_wallet_address"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    chain: Mapped[str] = mapped_column(String(10))     # "BTC" | "ETH"
    address: Mapped[str] = mapped_column(String(120))  # on-chain address (native format per chain)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)  # optional user label
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CryptoValueSnapshot(Base):
    """A dated total-crypto-value snapshot for one portfolio (metron-ops#111).

    Forward-recorded once per UTC day from the synced ``crypto/holdings.json`` total, like
    ``NavSnapshot`` for equities — crypto value can't be reconstructed for the past from the
    current chain balance alone, so history accrues one day at a time. Recorded idempotently
    when the crypto page is viewed with a fresh artifact; never fabricated when the feed is
    absent. Tenant + portfolio scoped; one row per (portfolio, snap_date)."""

    __tablename__ = "crypto_value_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "portfolio_id", "snap_date", name="uq_crypto_value_snapshot_day"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    snap_date: Mapped[date] = mapped_column(index=True)
    value_usd: Mapped[float] = mapped_column(Numeric(28, 10))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
