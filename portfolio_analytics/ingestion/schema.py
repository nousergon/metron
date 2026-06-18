"""Canonical broker-data schema — the common template every connector syncs into.

This is the **silver** layer of the connector ingestion pipeline (bronze = raw
broker payloads in ``connectors.store``; gold = the dashboard's view aggregates).
Every connector maps its broker-specific shape into these types so the dashboard
reads one schema regardless of source — the broker-agnostic substrate behind a
menu of connectors.

Modeled on the **Financial Data Exchange (FDX v6.5)** investment data cluster and
Plaid's Investments model: a universal **Security** master (instrument identity) is
kept separate from the account-specific **Holding**, and both holdings and
activities reference a security by ``security_id``. Currencies are ISO-4217. Pure
dataclasses (no pydantic), mirroring ``analytics.ledger``.

Sign convention (``CanonicalActivity.amount``): a **positive magnitude**, with
``type`` carrying direction — identical to ``analytics.ledger.Transaction`` (the
sole downstream consumer). This deliberately departs from Plaid's *signed*
convention (sale = inflow negative, buy = outflow positive): adopting Plaid's signs
would force a sign-flip at the one boundary that matters (canonical → Transaction),
so matching the consumer is the correct, lower-friction choice. Native ``amount`` +
``currency`` are preserved so income-by-year can FX-convert later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from portfolio_analytics.domain.ledger import RealizedGain, TxnType  # noqa: F401 — re-exported for connectors

SCHEMA_VERSION = "1"

# Asset categories carried on a security (broker-agnostic).
ASSET_EQUITY = "EQUITY"
ASSET_ETF = "ETF"
ASSET_FUND = "FUND"
ASSET_OPTION = "OPTION"
ASSET_CASH = "CASH"
ASSET_OTHER = "OTHER"


def synth_security_id(ticker: str, currency: str = "USD") -> str:
    """Stable synthetic ``security_id`` for an equity a broker gives no native id for.

    ``EQ:<TICKER>:<CCY>`` — ticker + ISO-4217 currency disambiguates dual listings
    (e.g. a US vs HK line of the same name). Brokers that expose a CUSIP/ISIN/conid
    should set their own ``security_id`` + ``security_id_type`` instead.
    """
    return f"EQ:{ticker.upper()}:{currency.upper()}"


@dataclass(frozen=True)
class CanonicalSecurity:
    """Universal instrument identity (FDX ``Security`` / Plaid ``security``).

    ``security_id`` is the stable join key referenced by holdings + activities.
    """

    security_id: str
    ticker: str = ""
    name: str = ""
    currency: str = "USD"  # ISO-4217
    asset_type: str = ASSET_EQUITY
    security_id_type: str = "TICKER"  # TICKER | CUSIP | ISIN | CONID
    exchange: str = ""  # broker listing exchange (e.g. "SEHK", "LSE") — drives yfinance symbology


@dataclass(frozen=True)
class CanonicalAccount:
    """An account at a brokerage. ``number`` is the canonical join key (never a
    SnapTrade-style opaque ``id``). ``nav_usd`` is the authoritative total value in
    base/USD (FX-correct, the analog of SnapTrade's ``balance_total``); ``cash_usd``
    is the cash plug ``nav_usd − positions_usd`` so it reconciles to ``nav_usd``."""

    number: str
    label: str = ""
    institution: str = ""
    tax_treatment: str = ""  # "" | taxable | tax_deferred | tax_exempt — seeds resolve_meta
    nav_usd: float = 0.0
    cash_usd: float = 0.0
    currency: str = "USD"
    as_of: datetime | None = None
    source: str = ""  # connector name that produced this record
    # Broker-native fields preserved for display fidelity (FDX accounts carry an
    # accountId + name + type alongside the number). ``account_id`` defaults to the
    # number when a broker exposes no separate id (e.g. IBKR Flex).
    account_id: str = ""
    name: str = ""  # broker's account name (maps to SnapTrade ``name``)
    account_type: str = ""  # broker account type (maps to SnapTrade ``institution_type``)


@dataclass(frozen=True)
class CanonicalHolding:
    """An account-specific position. Values are **native** currency — the display
    layer (``load_portfolio._enrich``) applies its own FX + live-price overlay, so
    feeding base-converted values here would double-convert."""

    account_number: str
    security_id: str
    quantity: float = 0.0
    cost_basis: float = 0.0  # total native cost basis
    avg_cost: float = 0.0  # native per-share
    market_value_local: float = 0.0  # native MV (recomputed with live price for display)
    currency: str = "USD"
    as_of: datetime | None = None
    source: str = ""


@dataclass(frozen=True)
class CanonicalOpenLot:
    """A single still-open tax lot of a holding, from broker lot-level Open Positions.

    Carries the lot's ``open_date`` so the historical position timeline — and thus a real
    NAV/TWR history — can be reconstructed for snapshot-sourced accounts that have no
    replayable trade feed (metron-ops#74). Native currency; replaced per account each sync
    (snapshot semantics, like holdings)."""

    account_number: str
    security_id: str
    ticker: str
    quantity: float
    open_date: date
    cost_basis: float = 0.0  # total native cost of this lot
    currency: str = "USD"


@dataclass(frozen=True)
class CanonicalActivity:
    """A dated account event (trade / dividend / interest / fee / transfer).

    ``amount`` is a positive magnitude in native ``currency``; ``type`` carries
    direction (see the module docstring sign-convention note). ``quantity`` /
    ``price`` apply to BUY/SELL; ``amount`` to cash events."""

    account_number: str
    when: date
    type: TxnType
    security_id: str = ""
    quantity: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    fees: float = 0.0
    currency: str = "USD"
    as_of: datetime | None = None
    source: str = ""


def activity_key(act: CanonicalActivity) -> str:
    """Stable identity for cross-sync dedup (events accumulate beyond a rolling
    window, so they're unioned, not replaced)."""
    return f"{act.account_number}|{act.when.isoformat()}|{act.type}|{act.security_id}|{act.quantity}|{act.amount}"


def lot_key(account_number: str, rg: RealizedGain) -> str:
    """Stable identity for a closed lot — mirrors ``loaders.ibkr_flex._lot_key``."""
    return f"{account_number}|{rg.ticker}|{rg.open_date}|{rg.close_date}|{rg.quantity}|{rg.proceeds}"
