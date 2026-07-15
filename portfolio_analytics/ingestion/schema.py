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

# Producer-side asset-category vocabulary (IBKR Flex ``assetCategory`` + the
# reference-rate contract mirror it) mapped to the canonical ASSET_* constants
# above. Shared so every connector classifies the same category string the same
# way instead of each re-deriving its own mapping.
_ASSET_CATEGORY_MAP = {
    "STK": ASSET_EQUITY,
    "ETF": ASSET_ETF,
    "FUND": ASSET_FUND,
    "MF": ASSET_FUND,
}


def asset_type_from_category(category: str | None) -> str:
    """Map a producer-supplied asset-category string to a canonical ASSET_* constant.

    Unknown/missing categories default to ``ASSET_OTHER`` — never silently to
    ``ASSET_EQUITY`` — so an unrecognized category is visibly distinct rather than
    masquerading as a stock.
    """
    return _ASSET_CATEGORY_MAP.get((category or "").upper(), ASSET_OTHER)


# 3-way tax-treatment vocabulary (mirrors ``api.db.models.Account.tax_treatment``).
TAX_TAXABLE = "taxable"
TAX_DEFERRED = "tax_deferred"
TAX_EXEMPT = "tax_exempt"

# Producer-side structured account-type vocabulary → the canonical 3-way tax
# treatment, keyed by the broker's OWN account-type field (IBKR Flex's
# ``accountType``, SnapTrade's ``type``) rather than free-text name/nickname —
# metron-ops#194. Both connectors are believed to draw from the same small,
# broker-controlled enum (IRA-family individual retirement account subtypes), so one
# shared table avoids the two connectors drifting on the same broker-reported string.
# Split into three keyword sets (rather than one flat dict) so resolution can check
# EXEMPT first, then DEFERRED, then TAXABLE — mirroring account_meta.py's own
# precedence (its docstring: "exempt keywords checked first, so 'roth ira' never
# matches the bare 'ira'"). A flat longest-substring-wins scan is NOT safe here: a
# broker-appended qualifier like "Roth IRA - Individual" contains both "roth ira"
# (8 chars, exempt) and "individual" (10 chars, taxable) — length alone would pick
# the wrong one. Checking exempt/deferred signals before the generic taxable ones
# fixes that regardless of phrase length. Unrecognized strings deliberately map to ""
# (not a guess) — ``account_meta.is_taxable``/``is_tax_deferred`` keyword-infer from
# account_type/name as the documented fallback for exactly this case.
_TAX_EXEMPT_TYPES = (
    "roth ira", "roth 401k", "roth 401(k)", "roth", "hsa", "529", "tfsa",
)
_TAX_DEFERRED_TYPES = (
    "traditional ira", "rollover ira", "sep ira", "simple ira", "ira",
    "401k", "401(k)", "403b", "403(b)", "457b", "457", "pension", "annuity", "rrsp",
)
_TAX_TAXABLE_TYPES = (
    "individual brokerage", "individual", "joint", "brokerage", "taxable",
    "trust", "custodial", "ugma", "utma", "corporate", "llc", "partnership",
)


def tax_treatment_from_account_type(account_type: str | None) -> str:
    """Positively derive the 3-way ``tax_treatment`` from a broker's OWN structured
    account-type field (IBKR Flex ``accountType`` / SnapTrade ``type``) — the
    root-cause fix for metron-ops#194: neither connector previously populated
    ``tax_treatment`` at all, leaving every account on the keyword-inference fallback.

    Case-insensitive substring match, checked EXEMPT → DEFERRED → TAXABLE (each set's
    own phrases checked longest-first) so a qualifier the broker appends ("Roth IRA -
    Individual") resolves to the more specific/diagnostic "roth ira" signal rather
    than a coincidentally-longer generic word. Returns "" (never a guess) when the
    type is unrecognized — callers then fall through to ``account_meta``'s
    keyword-inference fallback, as documented there.
    """
    t = (account_type or "").strip().lower()
    if not t:
        return ""
    for keywords, treatment in (
        (_TAX_EXEMPT_TYPES, TAX_EXEMPT),
        (_TAX_DEFERRED_TYPES, TAX_DEFERRED),
        (_TAX_TAXABLE_TYPES, TAX_TAXABLE),
    ):
        for key in keywords:
            if key in t:
                return treatment
    return ""


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
