"""Portfolios router — CRUD + file ingestion + ledger-derived analytics (PH1).

Tenant resolution is stubbed (``X-Tenant-Id`` header) until auth lands in PH4; the
real product derives the tenant from the authenticated session.

PH1 adds the ingestion round-trip the free beta is built on. Three sources land
through one canonical pipeline + persistence bridge: ``POST …/import/csv`` and
``…/import/ofx`` (transaction-sourced — a trade history the ledger reconstructs
positions from) and ``…/import/flex`` (snapshot-sourced — IBKR reports current
positions directly). ``GET …/holdings`` unions both models; ``…/transactions`` /
``…/realized`` read the ledger view back out. Price-dependent analytics (market
value, performance, risk) arrive in later PH1–PH3 increments.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.db.session import get_session
from api.services import (
    account_meta,
    analytics,
    attribution,
    calendar,
    crypto,
    data_spine,
    indices,
    intraday,
    labels,
    performance,
    persistence,
    risk,
    security_perf,
    tax,
    watchlist,
)
from api.services import analyst as analyst_service
from api.services import attractiveness as attractiveness_service
from api.services import (
    classifications as classifications_service,
)
from api.services import (
    countries as countries_service,
)
from api.services import fundamentals as fundamentals_service
from api.services import fx as fx_service
from api.services import prices as price_service
from api.services import (
    sectors as sectors_service,
)
from api.services import sentiment as sentiment_service
from api.services import (
    tearsheet as tearsheet_service,
)
from api.services import technicals as technicals_service
from api.services import valuation_medians as valuation_medians_service
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.broker_io.file_import import FileImportError, FileImportResult
from portfolio_analytics.broker_io.ofx_import import parse_ofx
from portfolio_analytics.broker_io.snaptrade_reader import SnapTradeReader
from portfolio_analytics.ingestion.ibkr_flex_connector import IbkrFlexConnector
from portfolio_analytics.ingestion.snaptrade import SnapTradeConnector

router = APIRouter(prefix="/portfolios", tags=["portfolios"])

# Cap the per-row error detail echoed back so a pathologically dirty upload can't
# return a multi-megabyte body; the total skipped count is always exact.
_MAX_ERROR_DETAIL = 100


class PortfolioIn(BaseModel):
    name: str
    base_currency: str = "USD"


class PortfolioOut(BaseModel):
    id: uuid.UUID
    name: str
    base_currency: str


class PortfolioRename(BaseModel):
    # Both optional so the Settings page can PATCH name and/or base currency; at least
    # one must be provided.
    name: str | None = None
    base_currency: str | None = None


class AccountTagsIn(BaseModel):
    """Editable account tags from the Settings page. The PATCH handler reads
    ``model_fields_set`` so an omitted field is left as-is, while an explicitly-sent
    field (including ``taxable_override: null``, which reverts to auto-derivation) is
    applied."""

    model_config = ConfigDict(extra="forbid")

    nickname: str | None = None
    institution: str | None = None
    account_type: str | None = None
    # The 3-way type (taxable | tax_deferred | tax_exempt). Setting it clears any binary
    # taxable_override so the 3-way is the single governing control; null = Auto-derive.
    tax_treatment: str | None = None
    taxable_override: bool | None = None


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_tolerance: str | None = None
    objective: str | None = None
    notes: str | None = None
    # The single user-facing intraday-overlay switch (default OFF). Full-replace PUT, so a
    # client that loaded `current` and submits the whole object governs this too.
    intraday_enabled: bool | None = None


class PreferencesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_tolerance: str | None = None
    objective: str | None = None
    notes: str | None = None
    intraday_enabled: bool | None = None


class HoldingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    quantity: float
    avg_cost: float            # native per-share cost
    cost_basis: float          # native total cost basis
    currency: str = "USD"
    fx_rate: float | None = None          # base per 1 unit of `currency` (1.0 for USD)
    # Null on the price-free path; populated from the cached close (see prices/refresh).
    # ``_local`` fields are native; the bare market_value/cost_basis_base are base-currency.
    last_price: float | None = None
    last_price_date: date | None = None
    # True when the close-fed last_price is ≥1 full trading session stale (upstream EOD
    # feed stalled). Drives the Holdings "prices as of" staleness warning. Always False on
    # the broker-snapshot / intraday-overlay paths (see security_perf.enrich_holdings).
    last_price_stale: bool = False
    # True when last_price is a same-day ESTIMATE synthesized from a tracking-proxy ETF's
    # return (metron-ops#112 mechanism B) — a late-striking mutual fund that hasn't struck
    # its own NAV yet today. Not a problem flag like last_price_stale — an expected,
    # clearly-labeled estimate, reconciled to the true struck NAV by mechanism A tomorrow.
    is_estimated: bool = False
    market_value_local: float | None = None
    cost_basis_base: float | None = None
    market_value: float | None = None
    unrealized_gain: float | None = None
    unrealized_pct: float | None = None
    # Coarse asset class for grouping (cash / bond / equity / etf / fund / option / other).
    security_type: str = "other"
    # Account attribution — set only on the ?by_account=1 (uncombined) view, where one row
    # is one (account, ticker). Null on the default consolidated view (metron-ops#114).
    account_id: uuid.UUID | None = None
    account_label: str | None = None
    # User-set display label/alias (so a numeric-CUSIP bond is legible). None when unset.
    user_label: str | None = None
    # Per-security period returns (metron-ops#87). Day legs (overnight/intraday/day) need the
    # intraday feed → null off a feed-entitled build; YTD/LTM from cached daily closes.
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    ytd_pct: float | None = None
    ltm_pct: float | None = None
    # Reference classification (cached from the data spine). GICS sector + country of
    # domicile; country drives the Holdings US-vs-international split. None = unclassified
    # coverage gap, never guessed.
    sector: str | None = None
    country: str | None = None
    # Valuation / fundamentals / technicals metrics (Holdings metrics) — feed-gated
    # (yfinance data spine). None off a feed-entitled build or on a coverage gap.
    market_cap: float | None = None
    pe: float | None = None
    fwd_pe: float | None = None
    pb: float | None = None
    ps: float | None = None
    ev_ebitda: float | None = None
    peg: float | None = None
    div_yield: float | None = None
    rev_growth: float | None = None
    earnings_growth: float | None = None
    gross_margin: float | None = None
    op_margin: float | None = None
    roe: float | None = None
    roa: float | None = None
    beta: float | None = None
    cash: float | None = None
    debt: float | None = None
    net_debt: float | None = None
    debt_to_equity: float | None = None
    net_debt_to_ebitda: float | None = None
    current_ratio: float | None = None
    quick_ratio: float | None = None
    fcf: float | None = None
    rsi_14: float | None = None
    macd_hist: float | None = None
    pct_to_ma_50: float | None = None
    pct_to_ma_200: float | None = None
    pct_in_52w_range: float | None = None
    mom_20d: float | None = None
    # Consensus research + news sentiment (metron-ops#105) — feed-gated (data spine, free
    # sources). None off a feed-entitled build or on a coverage gap, never fabricated.
    consensus_rating: str | None = None
    consensus_score: float | None = None
    price_target_mean: float | None = None
    price_target_median: float | None = None
    price_target_upside: float | None = None
    num_analysts: int | None = None
    news_sentiment: float | None = None
    news_articles: int | None = None
    # Composite attractiveness score (metron-ops#106, Phase 2) — transparent 0–100 blend of the
    # fields above. None off-feed or on a total coverage gap, never fabricated.
    attractiveness: float | None = None
    attractiveness_coverage: int | None = None


class GroupMediansOut(BaseModel):
    """Sector- or country-level median multiples (SP1500-broad peer benchmark) for the
    Holdings "by sector → country" bands. None fields = the producer had no usable sample."""
    model_config = ConfigDict(from_attributes=True)

    n: int = 0
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = None
    price_to_sales: float | None = None
    ev_ebitda: float | None = None
    dividend_yield: float | None = None


class ValuationMediansOut(BaseModel):
    """Median bands restricted to the sectors/countries the portfolio actually holds."""
    as_of: date | None = None
    by_sector: dict[str, GroupMediansOut] = {}
    by_country: dict[str, GroupMediansOut] = {}


class RealizedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    open_date: date
    close_date: date
    quantity: float
    proceeds: float          # native
    cost_basis: float        # native
    gain: float              # native
    long_term: bool
    currency: str = "USD"
    fx_rate: float | None = None       # close-date base-per-unit (1.0 for USD)
    gain_base: float | None = None     # gain in the base currency at the close-date rate
    proceeds_base: float | None = None
    cost_basis_base: float | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trade_date: date
    txn_type: str
    ticker: str
    quantity: float
    price: float
    amount: float
    fees: float
    currency: str


class IncomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    realized_st: float
    realized_lt: float
    dividends: float
    interest: float
    distributions: float = 0.0
    net_capital_gains: float
    taxable_income: float


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    broker: str
    external_id: str
    name: str
    currency: str
    nickname: str | None = None
    institution: str | None = None
    account_type: str | None = None
    tax_treatment: str | None = None
    taxable: bool = True
    # Per-account valuation (base currency); None until priced (cost basis is price-free).
    cost_basis_base: float | None = None
    market_value: float | None = None
    unrealized_gain: float | None = None
    n_unconverted: int = 0
    # Per-account period returns (metron-ops#87). Day legs need the intraday feed; YTD/LTM
    # from the per-account reconstructed NAV series. Null when unavailable.
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    ytd_pct: float | None = None
    ltm_pct: float | None = None


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_currency: str
    n_accounts: int
    n_holdings: int
    total_cost_basis: float
    realized_st: float
    realized_lt: float
    realized_total: float
    realized_st_ytd: float = 0.0
    realized_lt_ytd: float = 0.0
    realized_ytd_taxadv: float = 0.0
    dividends: float
    interest: float
    distributions: float = 0.0
    taxable_income: float
    market_value: float | None = None
    unrealized_gain: float | None = None
    n_unconverted: int = 0


class IntradayStatusOut(BaseModel):
    """Live-valuation status for the Overview/Holdings/Performance "intraday" label +
    poll (metron-ops#79)."""

    applied: bool                  # the intraday overlay is currently in effect
    as_of_utc: str | None = None   # producer write time of the snapshot in use
    stale: bool = False            # snapshot older than the freshness window (market closed?)
    n_priced: int = 0              # held positions revalued from a fresh intraday quote
    reason: str | None = None      # why not applied ("feed" / "stale" / "unavailable")


class TodayRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    label: str
    quantity: float
    currency: str
    prev_close: float | None = None
    open: float | None = None
    last: float | None = None
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    overnight_gain: float | None = None
    intraday_gain: float | None = None
    day_gain: float | None = None


class TodayOut(BaseModel):
    """The Today view — per-holding overnight·intraday·day decomposition + totals
    (metron-ops#23)."""

    model_config = ConfigDict(from_attributes=True)

    available: bool
    base_currency: str
    reason: str | None = None
    as_of_utc: str | None = None
    stale: bool = False
    n_priced: int = 0
    n_excluded: int = 0
    overnight_gain: float | None = None
    intraday_gain: float | None = None
    day_gain: float | None = None
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None
    rows: list[TodayRowOut] = []


class AccountDetailOut(BaseModel):
    """One account's holdings + activity — the per-account drill-down, in one call.

    Every figure is scoped to this single account (its own ledger + broker-reported
    positions), so a multi-account portfolio breaks down cleanly. Price-free, like the
    portfolio-level views."""

    account: AccountOut
    holdings: list[HoldingOut]
    realized: list[RealizedOut]
    transactions: list[TransactionOut]


class PriceRefreshOut(BaseModel):
    """Result of refreshing the EOD price cache for a portfolio's held tickers."""

    symbols_requested: int
    prices_updated: int
    snapshot_recorded: bool


class PerfPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    snap_date: date
    nav: float
    external_flow: float
    spy_close: float | None


class RollingRiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    snap_date: date
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None


class PerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    n_snapshots: int
    first_date: date | None
    last_date: date | None
    days: int
    latest_nav: float | None
    latest_cost_basis: float | None
    net_contributions: float
    cumulative_return: float | None
    twr: float | None
    annualized_twr: float | None
    volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    spy_return: float | None
    alpha: float | None
    rolling: list[RollingRiskOut] = []
    points: list[PerfPointOut]
    estimated: bool = False
    estimated_note: str | None = None


class TaxLotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    open_date: date
    quantity: float
    currency: str = "USD"
    cost_basis: float                    # native total cost basis
    term: str
    cost_basis_base: float | None = None  # base-currency cost basis
    market_value: float | None           # base-currency market value
    unrealized_gain: float | None
    harvestable_loss: float | None


class TaxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    as_of: date
    base_currency: str = "USD"
    n_lots: int
    n_priced: int
    unrealized_st: float | None
    unrealized_lt: float | None
    unrealized_total: float | None
    unrealized_position_total: float | None = None
    harvestable_loss: float | None
    n_accounts_excluded: int = 0
    n_incomplete: int = 0
    incomplete_tickers: list[str] = []
    lots: list[TaxLotOut]


class RiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    computable: bool
    benchmark: str
    reason: str | None
    required_tier: str | None = None
    as_of: date | None
    n_obs: int
    n_modeled: int
    excluded: list[str]
    total_vol: float | None
    factor_vol: float | None
    idio_vol: float | None
    idio_pct: float | None
    tracking_error: float | None
    factor_exposures: dict[str, float]
    factor_pct_contrib: dict[str, float]


class SectorEffectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sector: str
    port_weight: float
    bench_weight: float
    port_return: float | None
    bench_return: float | None
    allocation: float
    selection: float
    interaction: float
    total: float


class AttributionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    computable: bool
    benchmark: str
    reason: str | None
    required_tier: str | None = None
    as_of: date | None
    start_date: date | None
    lookback_days: int
    coverage: float
    n_sectors: int
    portfolio_return: float | None
    benchmark_return: float | None
    active_return: float | None
    allocation: float | None
    selection: float | None
    interaction: float | None
    sectors: list[SectorEffectOut]


class CalendarEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_date: date
    kind: str
    ticker: str
    label: str


class CalendarOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    as_of: date
    horizon_days: int
    n_events: int
    events: list[CalendarEventOut]


class WatchlistEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    name: str | None = None
    sector: str | None = None
    next_earnings_date: date | None = None
    held: bool = False
    note: str | None = None


class WatchlistIn(BaseModel):
    symbol: str
    note: str | None = None


class WatchlistDeleteOut(BaseModel):
    symbol: str
    removed: bool


class SecurityLabelIn(BaseModel):
    # An empty/blank label CLEARS the alias (reverts to showing the raw symbol).
    label: str | None = None


class SecurityLabelOut(BaseModel):
    symbol: str
    label: str | None = None


class SecurityClassificationIn(BaseModel):
    # A user-set sector / country / instrument_type override (fills/corrects a holding's
    # classification). Only the fields PRESENT in the request body are changed — an omitted
    # field keeps its stored value, while an explicit null/empty CLEARS that field (and
    # clearing all deletes the override). ``model_fields_set`` distinguishes "omitted" from
    # "set to null".
    sector: str | None = None
    country: str | None = None
    instrument_type: str | None = None


class SecurityClassificationOut(BaseModel):
    symbol: str
    sector: str | None = None
    country: str | None = None
    instrument_type: str | None = None


class SkipOut(BaseModel):
    ref: str       # human locator: "line 4" (CSV) | "fitid T123" (OFX)
    reason: str


class ImportOut(BaseModel):
    source: str
    rows_parsed: int
    rows_skipped: int
    accounts_created: int
    securities_created: int
    transactions_inserted: int
    transactions_skipped: int
    positions_imported: int = 0
    errors: list[SkipOut]


class FlexImportIn(BaseModel):
    """IBKR Flex BYO-token credentials. Stateless: used for one fetch, never stored."""

    token: str
    query_id: str


def _tenant_id(x_tenant_id: str | None = Header(default=None)) -> uuid.UUID:
    # Placeholder for the authenticated tenant (PH4 replaces this with the session).
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-Id header required (auth lands in PH4)")
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="X-Tenant-Id must be a UUID") from e


def _effective_entitlement(
    feature_key: str,
    x_preview_tier: str | None,
    x_preview_feed: str | None,
) -> dict:
    """Resolve one feature's entitlement for the request's effective tier + feed.

    The effective tier/feed is this deployment's (``default_tier`` +
    ``feed_entitled`` — the entitlement feed axis, decoupled from the S3
    ``market_data_sync_enabled`` infra toggle per metron-ops#43). When the **tier
    simulator** is on (owner-only, never on the public product) the
    ``X-Preview-Tier`` / ``X-Preview-Feed`` headers override them — mirroring
    ``GET /meta/entitlements`` so a feed toggle in the simulator is honored
    server-side too. A bad preview tier falls back to the deployment default rather
    than 500-ing the compute call.

    Returns the per-feature dict from ``entitlements.resolve`` (``available`` /
    ``reason`` / ``required_tier`` / …) so callers can short-circuit a feed-dependent
    endpoint into an honest not-computable response when the tier excludes it.
    """
    tier = settings.default_tier
    feed = settings.feed_entitled
    if settings.tier_simulator:
        if x_preview_tier is not None:
            tier = x_preview_tier
        if x_preview_feed is not None:
            feed = x_preview_feed.strip().lower() == "true"
    try:
        resolved = ent.resolve(tier, feed_enabled=feed)
    except ValueError:
        resolved = ent.resolve(settings.default_tier, feed_enabled=settings.feed_entitled)
    return next(f for f in resolved["features"] if f["key"] == feature_key)


def _external_market_data_allowed(x_preview_feed: str | None) -> bool:
    """Whether this request may read EXTERNAL market data — the S3 data-spine (whose EOD
    closes / sectors / earnings are ultimately yfinance-derived upstream) or a licensed
    feed. This is the feed-entitlement axis (``feed_entitled``), honoring the owner tier
    simulator's feed preview.

    The **beta tier makes ZERO yfinance-derived calls** (metron-ops#52): it values holdings
    from BROKER-supplied prices only (read paths use the cache + the broker fallback — no
    network), so the spine-reading REFRESH endpoints (price refresh / build-history /
    calendar refresh) are gated here and never run for a beta (feed-off) deployment. A
    licensed feed is a Pro-tier thing, not a beta cost."""
    feed = settings.feed_entitled
    if settings.tier_simulator and x_preview_feed is not None:
        feed = x_preview_feed.strip().lower() == "true"
    return feed


def _require_external_market_data(x_preview_feed: str | None) -> None:
    """Refuse a spine/feed-reading endpoint when the feed entitlement is off (the beta).
    Keeps yfinance-derived market data off every beta surface (metron-ops#52)."""
    if not _external_market_data_allowed(x_preview_feed):
        raise HTTPException(
            status_code=403,
            detail="Live market-data refresh needs the Pro feed; the beta values holdings from your broker.",
        )


@router.get("", response_model=list[PortfolioOut])
def list_portfolios(
    tenant_id: uuid.UUID = Depends(_tenant_id),
    session: Session = Depends(get_session),
) -> list[models.Portfolio]:
    rows = session.scalars(select(models.Portfolio).where(models.Portfolio.tenant_id == tenant_id)).all()
    return list(rows)


@router.post("", response_model=PortfolioOut, status_code=201)
def create_portfolio(
    body: PortfolioIn,
    tenant_id: uuid.UUID = Depends(_tenant_id),
    session: Session = Depends(get_session),
) -> models.Portfolio:
    # Ensure the tenant row exists (idempotent for the skeleton/dev flow).
    if session.get(models.Tenant, tenant_id) is None:
        session.add(models.Tenant(id=tenant_id, name=f"tenant-{tenant_id}"))
    portfolio = models.Portfolio(tenant_id=tenant_id, name=body.name, base_currency=body.base_currency)
    session.add(portfolio)
    session.commit()
    session.refresh(portfolio)
    return portfolio


def _owned_portfolio(
    portfolio_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(_tenant_id),
    session: Session = Depends(get_session),
) -> models.Portfolio:
    """Resolve a portfolio the caller's tenant owns, or 404 (never leak cross-tenant
    existence — a portfolio of another tenant is indistinguishable from a missing one)."""
    portfolio = session.scalars(
        select(models.Portfolio).where(
            models.Portfolio.id == portfolio_id,
            models.Portfolio.tenant_id == tenant_id,
        )
    ).first()
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    # The app is being actively used — touch the data-spine UI heartbeat (throttled,
    # fail-soft, flag-gated) so the intraday quote producer runs only while someone
    # is actually looking AND has the intraday overlay enabled. Every authenticated
    # portfolio request flows through this dependency, making it the one natural
    # chokepoint for "Metron is open".
    data_spine.touch_ui_heartbeat(session=session, tenant_id=tenant_id, portfolio_id=portfolio.id)
    return portfolio


@router.get("/{portfolio_id}", response_model=PortfolioOut)
def get_portfolio(portfolio: models.Portfolio = Depends(_owned_portfolio)) -> models.Portfolio:
    """Fetch one portfolio the caller's tenant owns (404 otherwise)."""
    return portfolio


@router.patch("/{portfolio_id}", response_model=PortfolioOut)
def update_portfolio(
    body: PortfolioRename,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.Portfolio:
    """Update a portfolio's name and/or base (reporting) currency. Trims whitespace; an
    empty name or no-op body is rejected (422). Base currency is upper-cased ISO-4217."""
    if body.name is None and body.base_currency is None:
        raise HTTPException(status_code=422, detail="Nothing to update")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Portfolio name cannot be empty")
        portfolio.name = name
    if body.base_currency is not None:
        ccy = body.base_currency.strip().upper()
        if len(ccy) != 3:
            raise HTTPException(status_code=422, detail="Base currency must be a 3-letter ISO-4217 code")
        portfolio.base_currency = ccy
    session.commit()
    session.refresh(portfolio)
    return portfolio


@router.get("/{portfolio_id}/preferences", response_model=PreferencesOut)
def get_preferences(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.InvestorPreferences | PreferencesOut:
    """The portfolio's saved investor preferences (defaults when none saved yet)."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    return pref or PreferencesOut()


@router.put("/{portfolio_id}/preferences", response_model=PreferencesOut)
def put_preferences(
    body: PreferencesIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.InvestorPreferences:
    """Create or update the portfolio's investor preferences (one row per portfolio)."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    if pref is None:
        pref = models.InvestorPreferences(tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id)
        session.add(pref)
    pref.risk_tolerance = (body.risk_tolerance or "").strip() or None
    pref.objective = (body.objective or "").strip() or None
    pref.notes = (body.notes or "").strip() or None
    pref.intraday_enabled = bool(body.intraday_enabled)
    session.commit()
    session.refresh(pref)
    return pref


def _owned_account(
    account_id: uuid.UUID,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> models.Account:
    """Resolve an account within a portfolio the caller's tenant owns, or 404.

    Layers on ``_owned_portfolio`` so a cross-tenant or cross-portfolio account id is
    indistinguishable from a missing one (never leak existence)."""
    account = session.scalars(
        select(models.Account).where(
            models.Account.id == account_id,
            models.Account.portfolio_id == portfolio.id,
        )
    ).first()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _selected_account_ids(
    account_id: list[uuid.UUID] = Query(default=[]),
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> set[uuid.UUID] | None:
    """Resolve the ``?account_id=`` selection (repeatable) into a validated set, or None.

    Absent OR empty → None (whole portfolio; we never pass ``IN ()`` to SQL). Every
    requested id must belong to this portfolio — any unknown/cross-portfolio id 404s as a
    set (never leak which ids exist), mirroring ``_owned_account`` for the single-id path."""
    if not account_id:
        return None
    requested = set(account_id)
    found = set(
        session.scalars(
            select(models.Account.id).where(
                models.Account.portfolio_id == portfolio.id,
                models.Account.id.in_(requested),
            )
        ).all()
    )
    if found != requested:
        raise HTTPException(status_code=404, detail="Account not found")
    return requested


_TAX_TREATMENTS = {"taxable", "tax_deferred", "tax_exempt"}


@router.patch("/{portfolio_id}/accounts/{account_id}", response_model=AccountOut)
def update_account_tags(
    body: AccountTagsIn,
    account: models.Account = Depends(_owned_account),
    session: Session = Depends(get_session),
) -> analytics.AccountInfo:
    """Edit an account's tags (nickname / institution / type / 3-way tax treatment) from
    Settings.

    Only fields present in the request body are changed (omitted = leave as-is). Setting
    ``tax_treatment`` (taxable | tax_deferred | tax_exempt, or null for Auto) is the
    single 3-way control: it **clears any binary ``taxable_override``** so the two can't
    disagree. ``taxable_override`` may still be set directly (kept for back-compat).
    Returns the account with its recomputed ``taxable`` status."""
    fields = body.model_fields_set
    if "nickname" in fields:
        account.nickname = (body.nickname or "").strip() or None
    if "institution" in fields:
        account.institution = (body.institution or "").strip() or None
    if "account_type" in fields:
        account.account_type = (body.account_type or "").strip() or None
    if "tax_treatment" in fields:
        treatment = (body.tax_treatment or "").strip().lower() or None
        if treatment is not None and treatment not in _TAX_TREATMENTS:
            raise HTTPException(
                status_code=422,
                detail="tax_treatment must be one of taxable, tax_deferred, tax_exempt (or null)",
            )
        account.tax_treatment = treatment
        # The 3-way is authoritative — drop any stale binary override so is_taxable reads
        # straight from tax_treatment (taxable → True, tax_deferred/tax_exempt → False).
        account.taxable_override = None
    if "taxable_override" in fields:
        account.taxable_override = body.taxable_override
    session.commit()
    session.refresh(account)
    return analytics.AccountInfo(
        account_id=account.id,
        broker=account.broker,
        external_id=account.external_id,
        name=account.name or "",
        currency=account.currency,
        nickname=account.nickname,
        institution=account.institution,
        account_type=account.account_type,
        tax_treatment=account.tax_treatment,
        taxable=account_meta.is_taxable(account),
    )


class AccountDeleteOut(BaseModel):
    account_id: uuid.UUID
    # The broker:external_id exclusion key now blocking re-import (restore in Settings).
    excluded_key: str


@router.delete("/{portfolio_id}/accounts/{account_id}", response_model=AccountDeleteOut)
def delete_account(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account: models.Account = Depends(_owned_account),
    session: Session = Depends(get_session),
) -> AccountDeleteOut:
    """Delete a connected account and all its data (transactions, positions, NAV
    snapshots), and record its ``broker:external_id`` on the portfolio's exclusion
    list so no future import resurrects it — a broker connection often carries
    accounts the user doesn't track (e.g. emptied siblings), and without the
    exclusion the very next sync would silently re-create the row. Reversible:
    restore the key from Settings, then re-sync.

    Also prunes the id from the saved accounts-panel selection so a stale saved
    filter can't reference a gone account."""
    account_id = account.id
    key = persistence.account_key(account.broker, account.external_id)
    # AccountNavSnapshot has no ORM cascade from Account (and SQLite FK enforcement is
    # off), so its rows are deleted explicitly; transactions/positions cascade via ORM.
    session.execute(
        sa_delete(models.AccountNavSnapshot).where(models.AccountNavSnapshot.account_id == account_id)
    )
    pref = _get_or_create_preferences(session, portfolio)
    keys = {s.strip() for s in (pref.excluded_account_keys or "").split(",") if s.strip()}
    keys.add(key)
    pref.excluded_account_keys = ", ".join(sorted(keys))
    selected = [s.strip() for s in (pref.selected_account_ids or "").split(",") if s.strip()]
    if str(account_id) in selected:
        pref.selected_account_ids = ", ".join(s for s in selected if s != str(account_id)) or None
    session.delete(account)
    session.commit()
    return AccountDeleteOut(account_id=account_id, excluded_key=key)


class ExcludedAccountOut(BaseModel):
    key: str
    broker: str
    external_id: str


class ExcludedAccountsOut(BaseModel):
    excluded: list[ExcludedAccountOut]


@router.get("/{portfolio_id}/accounts/excluded", response_model=ExcludedAccountsOut)
def list_excluded_accounts(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ExcludedAccountsOut:
    """Broker accounts the user deleted — imports skip these keys. Shown in Settings
    so deletion is reversible (restore → next sync re-imports)."""
    keys = persistence.excluded_account_keys(session, portfolio.tenant_id, portfolio.id)
    out = []
    for key in sorted(keys):
        broker, _, external_id = key.partition(":")
        out.append(ExcludedAccountOut(key=key, broker=broker, external_id=external_id))
    return ExcludedAccountsOut(excluded=out)


class RestoreAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str


@router.post("/{portfolio_id}/accounts/excluded/restore", response_model=ExcludedAccountsOut)
def restore_excluded_account(
    body: RestoreAccountIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ExcludedAccountsOut:
    """Drop one key from the exclusion list — the next sync of its source re-imports
    the account. 404 on an unknown key (nothing to restore)."""
    pref = _get_or_create_preferences(session, portfolio)
    keys = {s.strip() for s in (pref.excluded_account_keys or "").split(",") if s.strip()}
    if body.key not in keys:
        raise HTTPException(status_code=404, detail="No such excluded account")
    keys.discard(body.key)
    pref.excluded_account_keys = ", ".join(sorted(keys)) or None
    session.commit()
    return list_excluded_accounts(portfolio, session)


class AccountSelectionOut(BaseModel):
    account_ids: list[uuid.UUID]


class AccountSelectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[uuid.UUID]


@router.get("/{portfolio_id}/accounts/selection", response_model=AccountSelectionOut)
def get_account_selection(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> AccountSelectionOut:
    """The saved accounts-panel selection (empty = whole portfolio). Ids that no
    longer resolve to an account (deleted since saving) are filtered out, so the
    saved filter degrades to the surviving accounts rather than 404ing pages."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    raw = [s.strip() for s in ((pref.selected_account_ids if pref else None) or "").split(",") if s.strip()]
    ids = [uuid.UUID(s) for s in raw]
    if not ids:
        return AccountSelectionOut(account_ids=[])
    alive = set(
        session.scalars(
            select(models.Account.id).where(
                models.Account.portfolio_id == portfolio.id, models.Account.id.in_(ids)
            )
        ).all()
    )
    return AccountSelectionOut(account_ids=[i for i in ids if i in alive])


@router.put("/{portfolio_id}/accounts/selection", response_model=AccountSelectionOut)
def put_account_selection(
    body: AccountSelectionIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> AccountSelectionOut:
    """Save the accounts-panel selection (empty list = whole portfolio, clears the
    saved filter). Every id must belong to this portfolio — unknown ids 404 as a set
    (mirrors ``_selected_account_ids``, never leaks which ids exist)."""
    requested = set(body.account_ids)
    if requested:
        found = set(
            session.scalars(
                select(models.Account.id).where(
                    models.Account.portfolio_id == portfolio.id,
                    models.Account.id.in_(requested),
                )
            ).all()
        )
        if found != requested:
            raise HTTPException(status_code=404, detail="Account not found")
    pref = _get_or_create_preferences(session, portfolio)
    pref.selected_account_ids = ", ".join(sorted(str(i) for i in body.account_ids)) or None
    session.commit()
    return AccountSelectionOut(account_ids=body.account_ids)


# Saved Holdings-table view (metron-ops#114). Valid grouping modes + metric bands — a saved
# value outside these sets is ignored on read (degrades to the default) and rejected on write.
_HOLDINGS_GROUPINGS = {"asset", "classification", "account"}
_HOLDINGS_BANDS = {"Score", "Valuation", "Fundamentals", "Balance Sheet", "Technicals", "Consensus"}
# Valid instrument-type override values — the set classify_security_type emits (metron-ops#115).
_INSTRUMENT_TYPES = {"cash", "treasury", "cd", "bond", "equity", "etf", "fund", "option", "other"}


class HoldingsViewOut(BaseModel):
    grouping: str | None = None
    visible_bands: list[str] | None = None
    combine_by_account: bool | None = None
    hidden_types: list[str] | None = None


class HoldingsViewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grouping: str | None = None
    visible_bands: list[str] | None = None
    combine_by_account: bool | None = None
    hidden_types: list[str] | None = None


@router.get("/{portfolio_id}/holdings-view", response_model=HoldingsViewOut)
def get_holdings_view(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> HoldingsViewOut:
    """The saved Holdings-table view (grouping / visible bands / combine). All-null when
    unset → the page applies its defaults. An unrecognized stored grouping/band is dropped
    rather than served, so a renamed band never breaks the page."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    if pref is None:
        return HoldingsViewOut()
    grouping = pref.holdings_grouping if pref.holdings_grouping in _HOLDINGS_GROUPINGS else None
    bands_raw = [b.strip() for b in (pref.holdings_visible_bands or "").split(",") if b.strip()]
    bands = [b for b in bands_raw if b in _HOLDINGS_BANDS] or None
    hidden_raw = [t.strip() for t in (pref.holdings_hidden_types or "").split(",") if t.strip()]
    hidden = [t for t in hidden_raw if t in _INSTRUMENT_TYPES] or None
    return HoldingsViewOut(
        grouping=grouping,
        visible_bands=bands,
        combine_by_account=pref.holdings_combine_by_account,
        hidden_types=hidden,
    )


@router.put("/{portfolio_id}/holdings-view", response_model=HoldingsViewOut)
def put_holdings_view(
    body: HoldingsViewIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> HoldingsViewOut:
    """Save the Holdings-table view. Each field is independently nullable — null clears that
    facet back to the default. Validates the grouping mode + band names so only known view
    state is persisted."""
    if body.grouping is not None and body.grouping not in _HOLDINGS_GROUPINGS:
        raise HTTPException(status_code=422, detail="Unknown grouping")
    if body.visible_bands is not None and any(b not in _HOLDINGS_BANDS for b in body.visible_bands):
        raise HTTPException(status_code=422, detail="Unknown metric band")
    # Hidden types are filtered to known values rather than rejected — an unrecognized held
    # type (a raw security_type key) simply isn't persisted, never a 422 on a best-effort save.
    hidden = [t for t in (body.hidden_types or []) if t in _INSTRUMENT_TYPES]
    pref = _get_or_create_preferences(session, portfolio)
    pref.holdings_grouping = body.grouping
    pref.holdings_visible_bands = ", ".join(body.visible_bands) if body.visible_bands else None
    pref.holdings_combine_by_account = body.combine_by_account
    pref.holdings_hidden_types = ", ".join(hidden) if hidden else None
    session.commit()
    return HoldingsViewOut(
        grouping=body.grouping,
        visible_bands=body.visible_bands,
        combine_by_account=body.combine_by_account,
        hidden_types=hidden or None,
    )


def _summarize(snapshot, persisted: persistence.PersistResult, *, parsed: int, skipped: int, errors) -> ImportOut:
    """Build the import summary from a persisted snapshot — one shape for every source."""
    return ImportOut(
        source=snapshot.source,
        rows_parsed=parsed,
        rows_skipped=skipped,
        accounts_created=persisted.accounts_created,
        securities_created=persisted.securities_created,
        transactions_inserted=persisted.transactions_inserted,
        transactions_skipped=persisted.transactions_skipped,
        positions_imported=persisted.positions_imported,
        errors=[SkipOut(ref=e.ref, reason=e.reason) for e in errors[:_MAX_ERROR_DETAIL]],
    )


def _persist_and_summarize(
    session: Session, portfolio: models.Portfolio, result: FileImportResult
) -> ImportOut:
    """Persist a parsed file through the shared bridge and build the import summary.

    One path for every file type — securities upsert, accounts upsert, transactions
    unioned by source_key (idempotent re-upload)."""
    persisted = persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=result.snapshot
    )
    return _summarize(result.snapshot, persisted, parsed=result.parsed, skipped=result.skipped, errors=result.errors)


@router.post("/{portfolio_id}/import/csv", response_model=ImportOut)
async def import_csv(
    file: UploadFile = File(...),
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ImportOut:
    """Ingest a broker transactions CSV into this portfolio.

    Parses with the header-flexible canonical importer, then persists through the
    shared bridge. Un-importable rows are reported, not fatal; a structurally invalid
    file (missing date/type column) returns 422.
    """
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # tolerate a BOM from spreadsheet exports
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=422, detail=f"CSV must be UTF-8 text: {e}") from e
    try:
        result = parse_transactions_csv(text)
    except FileImportError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _persist_and_summarize(session, portfolio, result)


@router.post("/{portfolio_id}/import/ofx", response_model=ImportOut)
async def import_ofx(
    file: UploadFile = File(...),
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ImportOut:
    """Ingest a broker OFX/QFX download into this portfolio.

    Parses the investment statement (ofxtools) into the same canonical snapshot as
    CSV and persists through the same bridge. Unsupported individual transactions are
    reported, not fatal; a file with no parseable investment statement returns 422.
    """
    raw = await file.read()
    try:
        result = parse_ofx(raw)  # ofxtools reads the OFX header's own encoding from bytes
    except FileImportError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _persist_and_summarize(session, portfolio, result)


@router.post("/{portfolio_id}/import/flex", response_model=ImportOut)
def import_flex(
    body: FlexImportIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ImportOut:
    """Sync an IBKR Activity Flex Query into this portfolio (BYO-token).

    The user supplies their own Flex token + query id; we fetch the statement once and
    persist it through the shared bridge — broker-reported **positions** land in the
    positions table, cash transactions (dividends/interest/fees) in transactions.
    Stateless by design: the token is used for this one fetch and **never stored**
    (no per-tenant credential at rest), and the raw statement is not cached to local
    disk (``persist_bronze=False``). A Flex/network failure returns 502.
    """
    return _run_flex_import(session, portfolio, body.token, body.query_id)


def _run_flex_import(session: Session, portfolio: models.Portfolio, token: str, query_id: str) -> ImportOut:
    """Fetch + persist one IBKR Flex statement — the shared body of the BYO-token import
    and the stored-credential sync. 502 on a Flex/network failure (always with a reason)."""
    connector = IbkrFlexConnector(token, query_id, persist_bronze=False)
    snapshot = connector.sync()
    if snapshot.error:
        raise HTTPException(status_code=502, detail=f"IBKR Flex fetch failed: {snapshot.error}")
    persisted = persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=snapshot
    )
    return _summarize(snapshot, persisted, parsed=len(snapshot.activities), skipped=0, errors=[])


@router.post("/{portfolio_id}/sync/flex", response_model=ImportOut)
def sync_flex(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ImportOut:
    """Sync IBKR Flex from the deployment's STORED token + query id (metron-ops#82) — the
    one-click counterpart to ``/import/flex`` (no paste). Mirrors the server-side SnapTrade
    sync: single-operator owner build only. 404 when no stored Flex credentials are
    configured (the UI then shows the BYO-token form instead)."""
    if not (settings.flex_token and settings.flex_query_id):
        raise HTTPException(status_code=404, detail="No stored IBKR Flex credentials on this deployment.")
    return _run_flex_import(session, portfolio, settings.flex_token, settings.flex_query_id)


def _snaptrade_reader_or_error() -> SnapTradeReader:
    """Gate + construct the personal SnapTrade reader, shared by every SnapTrade route.

    The SnapTrade connection is **server-side** — one operator SnapTrade user + linked
    brokerages from the ``SNAPTRADE_*`` env. Because that credential is shared by the
    whole process, every SnapTrade surface is gated behind ``snaptrade_personal``
    (single-operator mode) and 404s when off, so a multi-tenant deploy can never let
    one tenant reach another's brokerage data. M2's per-user connection-portal flow is
    the multi-tenant replacement, not this. 503 when env creds are missing."""
    if not settings.snaptrade_personal:
        raise HTTPException(status_code=404, detail="SnapTrade sync is not enabled on this deployment.")
    try:
        return SnapTradeReader.from_env()
    except KeyError as e:
        raise HTTPException(status_code=503, detail=f"SnapTrade not configured — missing {e}.") from e


def _get_or_create_preferences(session: Session, portfolio: models.Portfolio) -> models.InvestorPreferences:
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    if pref is None:
        pref = models.InvestorPreferences(tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id)
        session.add(pref)
    return pref


def _snaptrade_excluded_ids(session: Session, portfolio: models.Portfolio) -> set[str]:
    """Authorization ids of connections this portfolio's SnapTrade sync skips.

    Linked = synced by default; exclusion is the rare opt-out for a broker sourced
    elsewhere (e.g. IBKR via Flex — syncing it from SnapTrade too would double-count).
    Keyed by the connection's stable authorization id — never by institution-name
    matching, which proved fragile (SnapTrade reports "E-Trade" on accounts but
    "E*Trade" on the connection)."""
    pref = session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == portfolio.tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio.id,
        )
    ).first()
    raw = pref.snaptrade_excluded_connections if pref is not None else None
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


@router.post("/{portfolio_id}/import/snaptrade", response_model=ImportOut)
def import_snaptrade(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> ImportOut:
    """Sync the operator's linked SnapTrade brokerages into this portfolio.

    Imports every account of every linked connection, minus connections the
    portfolio has explicitly excluded (see ``_snaptrade_excluded_ids``).

    404 when personal mode is off / 503 unconfigured (see
    ``_snaptrade_reader_or_error``); 502 on a SnapTrade/network failure — always
    with a reason, never a silent blank.
    """
    reader = _snaptrade_reader_or_error()
    snapshot = SnapTradeConnector(reader).sync()
    if snapshot.error:
        raise HTTPException(status_code=502, detail=f"SnapTrade sync failed: {snapshot.error}")
    excluded_ids = _snaptrade_excluded_ids(session, portfolio)
    if excluded_ids:
        # The snapshot's accounts don't carry the authorization id, so map account
        # numbers to connections via the reader and drop the excluded ones.
        try:
            accounts = reader.get_accounts()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"SnapTrade accounts fetch failed: {e}") from e
        excluded_numbers = {
            a.get("number") for a in accounts if a.get("brokerage_authorization") in excluded_ids
        }
        snapshot.accounts = [a for a in snapshot.accounts if a.number not in excluded_numbers]
        snapshot.holdings = [h for h in snapshot.holdings if h.account_number not in excluded_numbers]
        snapshot.activities = [a for a in snapshot.activities if a.account_number not in excluded_numbers]
    persisted = persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=snapshot
    )
    return _summarize(snapshot, persisted, parsed=len(snapshot.activities), skipped=0, errors=[])


class SnapTradeConnectionOut(BaseModel):
    id: str
    brokerage: str
    disabled: bool = False
    n_accounts: int = 0
    # True when this portfolio's sync skips the connection (opt-out, default synced).
    excluded: bool = False


class SnapTradeConnectionsOut(BaseModel):
    connections: list[SnapTradeConnectionOut]
    # Imported SnapTrade accounts in this portfolio's DB. Zero while connections exist =
    # "linked but never synced" — the gap the UI nudges the user to close (metron-ops#21).
    n_synced_accounts: int = 0


class SnapTradeConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional SnapTrade broker slug to deep-link the portal straight to one
    # brokerage's login (e.g. "ETRADE"); omitted = the portal's brokerage picker.
    broker: str | None = None
    # Optional existing authorization id: opens the portal straight into
    # re-authenticating that connection (repairs a disabled one, no new slot).
    reconnect: str | None = None


class SnapTradeConnectUrlOut(BaseModel):
    redirect_uri: str


@router.get("/{portfolio_id}/snaptrade/connections", response_model=SnapTradeConnectionsOut)
def list_snaptrade_connections(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SnapTradeConnectionsOut:
    """The operator's linked SnapTrade brokerage connections, with account counts and
    whether this portfolio's sync excludes each (linked = synced by default).

    Same gating/error contract as the sync (404 flag-off / 503 unconfigured / 502
    upstream failure with a reason)."""
    reader = _snaptrade_reader_or_error()
    try:
        connections = reader.get_connections()
        accounts = reader.get_accounts()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SnapTrade connections fetch failed: {e}") from e
    excluded_ids = _snaptrade_excluded_ids(session, portfolio)
    out = [
        SnapTradeConnectionOut(
            id=c["id"],
            brokerage=c["brokerage"],
            disabled=c["disabled"],
            n_accounts=sum(1 for a in accounts if a.get("brokerage_authorization") == c["id"]),
            excluded=c["id"] in excluded_ids,
        )
        for c in connections
    ]
    # Count SnapTrade-sourced accounts already imported into this portfolio — lets the UI
    # spot a "linked but never synced" state (metron-ops#21).
    n_synced = session.scalar(
        select(func.count())
        .select_from(models.Account)
        .where(
            models.Account.tenant_id == portfolio.tenant_id,
            models.Account.portfolio_id == portfolio.id,
            models.Account.broker.like("snaptrade%"),
        )
    )
    return SnapTradeConnectionsOut(connections=out, n_synced_accounts=int(n_synced or 0))


@router.post("/{portfolio_id}/snaptrade/connect", response_model=SnapTradeConnectUrlOut)
def snaptrade_connect_url(
    body: SnapTradeConnectIn | None = None,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
) -> SnapTradeConnectUrlOut:
    """A short-lived SnapTrade connection-portal URL for the operator user.

    Opening it is how a NEW brokerage (E*TRADE, Schwab, …) gets linked or a broken
    connection repaired (``reconnect`` = authorization id) — SnapTrade hosts the
    brokerage login; no credentials touch Metron. Connections are created read-only.
    Same gating/error contract as the sync (404 flag-off / 503 unconfigured / 502
    upstream failure with a reason)."""
    reader = _snaptrade_reader_or_error()
    try:
        url = reader.get_login_url(
            broker=(body.broker if body else None),
            reconnect=(body.reconnect if body else None),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SnapTrade connect-link failed: {e}") from e
    return SnapTradeConnectUrlOut(redirect_uri=url)


class SnapTradeRemoveOut(BaseModel):
    removed: str


@router.delete("/{portfolio_id}/snaptrade/connections/{authorization_id}", response_model=SnapTradeRemoveOut)
def remove_snaptrade_connection(
    authorization_id: str,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SnapTradeRemoveOut:
    """Permanently delete a brokerage connection at SnapTrade (frees a plan slot).

    Irreversible at SnapTrade's side — the connection's accounts stop refreshing and
    re-linking later creates a brand-new connection through the portal. Data already
    persisted in Metron's DB is untouched. The UI confirms before calling this.
    Same gating/error contract as the sync (404 flag-off / 503 unconfigured / 502
    upstream failure with a reason)."""
    reader = _snaptrade_reader_or_error()
    try:
        reader.remove_connection(authorization_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SnapTrade connection removal failed: {e}") from e
    # A removed connection can't linger in the exclusion set (a re-link gets a new id).
    if authorization_id in _snaptrade_excluded_ids(session, portfolio):
        _set_connection_excluded(session, portfolio, authorization_id, excluded=False)
    return SnapTradeRemoveOut(removed=authorization_id)


class SnapTradeExclusionOut(BaseModel):
    id: str
    excluded: bool


def _set_connection_excluded(
    session: Session, portfolio: models.Portfolio, authorization_id: str, excluded: bool
) -> SnapTradeExclusionOut:
    """Persist a connection's sync opt-out on the portfolio's preferences row.

    Keyed by the stable authorization id — exact, no name matching. Idempotent."""
    if not settings.snaptrade_personal:
        raise HTTPException(status_code=404, detail="SnapTrade sync is not enabled on this deployment.")
    ids = _snaptrade_excluded_ids(session, portfolio)
    if excluded:
        ids.add(authorization_id)
    else:
        ids.discard(authorization_id)
    pref = _get_or_create_preferences(session, portfolio)
    pref.snaptrade_excluded_connections = ", ".join(sorted(ids)) or None
    session.commit()
    return SnapTradeExclusionOut(id=authorization_id, excluded=excluded)


@router.post(
    "/{portfolio_id}/snaptrade/connections/{authorization_id}/exclude",
    response_model=SnapTradeExclusionOut,
)
def exclude_snaptrade_connection(
    authorization_id: str,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SnapTradeExclusionOut:
    """Opt this connection out of the portfolio's SnapTrade sync.

    For a broker sourced elsewhere (e.g. IBKR via Flex) — syncing it from SnapTrade
    too would double-count. Future syncs skip its accounts; already-imported data
    stays. No SnapTrade call is made; this is pure local preference."""
    return _set_connection_excluded(session, portfolio, authorization_id, excluded=True)


@router.post(
    "/{portfolio_id}/snaptrade/connections/{authorization_id}/include",
    response_model=SnapTradeExclusionOut,
)
def include_snaptrade_connection(
    authorization_id: str,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SnapTradeExclusionOut:
    """Undo a connection's sync opt-out (linked connections sync by default)."""
    return _set_connection_excluded(session, portfolio, authorization_id, excluded=False)


@router.post("/{portfolio_id}/prices/refresh", response_model=PriceRefreshOut)
def refresh_prices(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> PriceRefreshOut:
    """Refresh the EOD price cache for this portfolio's currently-held tickers.

    Fetches the latest close per held ticker from the S3 data-spine (whose closes are
    yfinance-derived upstream) into the global ``price_bars`` cache, after which holdings/
    summary report market value + unrealized P&L. **Feed-gated (metron-ops#52): the beta
    tier values holdings from BROKER prices only and may not pull spine-sourced data — this
    endpoint 403s when the feed entitlement is off.** Idempotent; unpriceable tickers stay
    cost-basis-only."""
    _require_external_market_data(x_preview_feed)
    held = analytics.holdings(session, portfolio.tenant_id, portfolio.id)
    symbols = [h.ticker for h in held if h.ticker]
    updated = price_service.refresh_latest_prices(session, symbols)
    # Refresh FX for every non-base currency held, so foreign positions convert into the
    # base-currency NAV/market value instead of being dropped from the totals.
    base = portfolio.base_currency or "USD"
    currencies = sorted({h.currency for h in held if h.currency and h.currency != base})
    if currencies:
        fx_service.refresh_fx_rates(session, currencies, base=base)
    # Backfill FX *history* over the portfolio's foreign-transaction span so realized
    # gains + dividends convert at their as-of-date rate (not today's).
    txn_ccys, earliest = analytics.foreign_transaction_currencies(session, portfolio.tenant_id, portfolio.id, base=base)
    if txn_ccys and earliest is not None:
        fx_service.backfill_fx_rates(session, txn_ccys, earliest, date.today(), base=base)
    # Record today's NAV snapshot off the freshly-cached prices (the forward-recorded
    # performance series). None when nothing could be priced. (The heavier NAV-history
    # reconstruct + Risk/Attribution backfills run in the daily job and behind the
    # Performance "Build history" / Risk+Attribution "Compute" buttons — kept off this
    # interactive path so a refresh click stays fast.)
    snap = performance.record_snapshot(session, portfolio.tenant_id, portfolio.id, today=date.today())
    # Per-account NAV snapshots too (additive; starts the per-account history). Cheap —
    # reuses the just-cached prices via one grouped valuation.
    performance.record_account_snapshots(session, portfolio.tenant_id, portfolio.id, today=date.today())
    return PriceRefreshOut(symbols_requested=len(symbols), prices_updated=updated, snapshot_recorded=snap is not None)


@router.get("/{portfolio_id}/holdings", response_model=list[HoldingOut])
def get_holdings(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    by_account: bool = False,
    session: Session = Depends(get_session),
) -> list[analytics.Holding]:
    # Valued when a cached close exists for the ticker; cost-basis-only otherwise.
    # ``?account_id=`` (repeatable) scopes to the selected accounts; absent = all.
    # ``?by_account=1`` returns the UNCOMBINED view — one row per (account, ticker), each
    # tagged with account_id/account_label (metron-ops#114); default consolidates per ticker.
    # LIVE intraday overlay (metron-ops#79): during trading hours on a feed-entitled build,
    # each position revalues from the intraday last; otherwise this is None → EOD close.
    prices, meta = intraday.for_portfolio(
        session, portfolio.tenant_id, portfolio.id, feed_entitled=settings.feed_entitled, account_ids=account_ids
    )
    held = (
        analytics.valued_holdings_by_account_flat(
            session, portfolio.tenant_id, portfolio.id, account_ids=account_ids, prices=prices
        )
        if by_account
        else analytics.valued_holdings(
            session, portfolio.tenant_id, portfolio.id, account_ids=account_ids, prices=prices
        )
    )
    # Late-striking-fund same-day ESTIMATE flag (metron-ops#112 mechanism B): a held ticker
    # whose live price was synthesized from a tracking-proxy ETF (not a real intraday quote)
    # is flagged so the UI can label it clearly, rather than silently look like a real quote.
    if meta.estimated_tickers:
        for h in held:
            if h.ticker in meta.estimated_tickers:
                h.is_estimated = True
    # Per-security Day / YTD / LTM returns for the Holdings table (metron-ops#87).
    held = security_perf.enrich_holdings(
        session, portfolio.tenant_id, portfolio.id, held,
        as_of=date.today(), feed_entitled=settings.feed_entitled, account_ids=account_ids,
    )
    # GICS sector + country of domicile for the table columns + the performers/geo
    # section. Both are cached reference data from the data spine; ensure_* only sources
    # the still-NULL gaps (idempotent — no network once a symbol is classified).
    tickers = [h.ticker for h in held]
    sectors_service.ensure_sectors(session, tickers)
    countries_service.ensure_countries(session, tickers)
    sector_of = sectors_service.sectors_by_symbol(session, tickers)
    country_of = countries_service.countries_by_symbol(session, tickers)
    # Tenant-set overrides win over the spine-resolved value — they fill the gaps the source
    # couldn't classify (and correct any it got wrong) without mutating the shared securities
    # reference row.
    overrides = classifications_service.overrides_by_symbol(session, portfolio.tenant_id, tickers)
    for h in held:
        ov = overrides.get(h.ticker)
        h.sector = (ov.sector if ov and ov.sector else None) or sector_of.get(h.ticker)
        h.country = (ov.country if ov and ov.country else None) or country_of.get(h.ticker)
        # Type override (metron-ops#115) — corrects a misclassified instrument type; falls
        # back to the classify_security_type result already stamped on the holding.
        if ov and ov.instrument_type:
            h.security_type = ov.instrument_type
    # Valuation / fundamentals / technicals columns (Holdings metrics) — feed-gated, same as
    # the Day legs above: yfinance-derived spine artifacts (licensed) populate only on a
    # feed-entitled build; off-feed each metric stays None and the table shows "—".
    if settings.feed_entitled:
        _enrich_metrics(session, held)
    return held


def _enrich_metrics(session: Session, held: list[analytics.Holding]) -> None:
    """Fill each holding's valuation/fundamentals/technicals + consensus/sentiment fields
    from the data-spine fundamentals + technicals + analyst + sentiment artifacts (keyed by
    yf_symbol). Fail-soft: a missing artifact or absent symbol leaves the fields None
    (coverage gap, never fabricated)."""
    yf_map = tearsheet_service._yf_symbol_map(session, [h.ticker for h in held])
    funds = fundamentals_service.load_fundamentals().by_symbol
    techs = technicals_service.load_technicals().by_symbol
    analysts = analyst_service.load_analyst().by_symbol
    sentiments = sentiment_service.load_sentiment().by_symbol
    # Sector/country median multiples — the peer benchmark for the attractiveness valuation
    # component (metron-ops#106). Fail-soft: a missing artifact leaves medians empty → the
    # valuation component is simply dropped from the renormalized blend.
    medians = valuation_medians_service.load_valuation_medians()
    for h in held:
        yf = yf_map.get(h.ticker, h.ticker)
        f = funds.get(yf)
        if f is not None:
            h.market_cap = f.market_cap
            h.pe = f.trailing_pe
            h.fwd_pe = f.forward_pe
            h.pb = f.price_to_book
            h.ps = f.price_to_sales
            h.ev_ebitda = f.ev_ebitda
            h.peg = f.peg
            h.div_yield = f.dividend_yield
            h.rev_growth = f.revenue_growth
            h.earnings_growth = f.earnings_growth
            h.gross_margin = f.gross_margins
            h.op_margin = f.operating_margins
            h.roe = f.roe
            h.roa = f.roa
            h.beta = f.beta
            # Balance sheet: absolute balances + derived net debt / leverage.
            h.cash = f.total_cash
            h.debt = f.total_debt
            h.debt_to_equity = f.debt_to_equity
            h.current_ratio = f.current_ratio
            h.quick_ratio = f.quick_ratio
            h.fcf = f.free_cashflow
            if f.total_debt is not None and f.total_cash is not None:
                h.net_debt = f.total_debt - f.total_cash
                if f.ebitda not in (None, 0):
                    h.net_debt_to_ebitda = h.net_debt / f.ebitda
        t = techs.get(yf)
        if t is not None:
            h.rsi_14 = t.rsi_14
            h.macd_hist = t.macd_hist
            h.pct_to_ma_50 = t.pct_to_ma_50
            h.pct_to_ma_200 = t.pct_to_ma_200
            h.pct_in_52w_range = t.pct_in_52w_range
            h.mom_20d = t.mom_20d
        # Consensus research (metron-ops#105) — price-target upside derived vs the live price.
        a = analysts.get(yf)
        if a is not None:
            h.consensus_rating = a.consensus_rating
            h.consensus_score = a.rating_score
            h.price_target_mean = a.mean_target
            h.price_target_median = a.median_target
            h.num_analysts = a.num_analysts
            h.price_target_upside = a.target_upside(h.last_price)
        # News sentiment (metron-ops#105).
        s = sentiments.get(yf)
        if s is not None:
            h.news_sentiment = s.sentiment
            h.news_articles = s.n_articles
        # Composite attractiveness score (metron-ops#106, Phase 2) — a transparent blend of the
        # fields just set. The valuation leg bands fwd-P/E against the holding's sector median
        # (country median as a fallback), exactly as the Holdings "by sector → country" view
        # does. Components with no input drop out and the weights renormalize (never fabricated).
        sec_grp = medians.by_sector.get(h.sector) if h.sector else None
        cty_grp = medians.by_country.get(h.country) if h.country else None
        median_fwd_pe = (sec_grp.forward_pe if sec_grp else None)
        if median_fwd_pe is None and cty_grp is not None:
            median_fwd_pe = cty_grp.forward_pe
        att = attractiveness_service.compute(
            fwd_pe=h.fwd_pe,
            median_fwd_pe=median_fwd_pe,
            price_target_upside=h.price_target_upside,
            consensus_score=h.consensus_score,
            estimate_revision_trend=(a.estimate_revision_trend if a is not None else None),
            news_sentiment=h.news_sentiment,
        )
        if att is not None:
            h.attractiveness = att.score
            h.attractiveness_coverage = att.coverage


@router.get("/{portfolio_id}/valuation-medians", response_model=ValuationMediansOut)
def get_valuation_medians(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> ValuationMediansOut:
    """SP1500-broad sector & country median multiples, restricted to the sectors/countries
    the portfolio actually holds — the peer benchmark for the Holdings "by sector → country"
    median bands. Feed-gated (yfinance-derived spine): empty off a feed-entitled build."""
    if not settings.feed_entitled:
        return ValuationMediansOut()
    held = analytics.valued_holdings(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)
    tickers = [h.ticker for h in held]
    # Resolve each holding's sector/country exactly as get_holdings does (overrides win), so
    # the band keys match the rows' grouping.
    sectors_service.ensure_sectors(session, tickers)
    countries_service.ensure_countries(session, tickers)
    sector_of = sectors_service.sectors_by_symbol(session, tickers)
    country_of = countries_service.countries_by_symbol(session, tickers)
    overrides = classifications_service.overrides_by_symbol(session, portfolio.tenant_id, tickers)
    present_sectors, present_countries = set(), set()
    for h in held:
        ov = overrides.get(h.ticker)
        s = (ov.sector if ov and ov.sector else None) or sector_of.get(h.ticker)
        c = (ov.country if ov and ov.country else None) or country_of.get(h.ticker)
        if s:
            present_sectors.add(s)
        if c:
            present_countries.add(c)
    snap = valuation_medians_service.load_valuation_medians()
    return ValuationMediansOut(
        as_of=snap.as_of,
        by_sector={k: v for k, v in snap.by_sector.items() if k in present_sectors},
        by_country={k: v for k, v in snap.by_country.items() if k in present_countries},
    )


def _taxable_scoped(
    session: Session, portfolio: models.Portfolio, account_ids: set[uuid.UUID] | None, taxable_only: bool
) -> set[uuid.UUID] | None:
    """Narrow the account scope to TAXABLE accounts when ``taxable_only`` (metron-ops#48).
    Tax-advantaged accounts (IRA / 401(k) / Roth) generate no taxable income or gains, so
    the Tax/Activity views default to taxable accounts. Intersects the current selection
    with the taxable set (or returns the full taxable set when nothing is selected)."""
    if not taxable_only:
        return account_ids
    taxable = account_meta.taxable_account_ids(session, portfolio.tenant_id, portfolio.id)
    return (account_ids & taxable) if account_ids is not None else taxable


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionOut])
def get_transactions(
    taxable_only: bool = False,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list[analytics.TransactionRow]:
    """Transaction ledger. ``taxable_only`` restricts to taxable accounts (the Activity
    view's default — most users only care about taxable events)."""
    account_ids = _taxable_scoped(session, portfolio, account_ids, taxable_only)
    return analytics.transactions(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/realized", response_model=list[RealizedOut])
def get_realized(
    taxable_only: bool = False,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list[analytics.RealizedLot]:
    """Closed lots. ``taxable_only`` restricts to taxable accounts (a realized gain in an
    IRA/401(k)/Roth is never taxed)."""
    account_ids = _taxable_scoped(session, portfolio, account_ids, taxable_only)
    return analytics.realized(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/income", response_model=list[IncomeOut])
def get_income(
    taxable_only: bool = False,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list:
    """Per-year realized income (cap gains ST/LT + dividends + interest + tax-deferred
    distributions), newest first. ``taxable_only`` restricts the gains/dividends/interest
    to taxable accounts (the Tax view's default); tax-deferred WITHDRAWALS within scope
    are surfaced as their own taxable **distributions** column regardless (metron-ops#62 —
    "Trad IRA is still taxable for retirees")."""
    scoped = _taxable_scoped(session, portfolio, account_ids, taxable_only)
    # Distributions are sourced from tax-deferred accounts (which taxable_only excludes),
    # intersected with the panel selection so the scope still honors what's selected.
    deferred = account_meta.tax_deferred_account_ids(session, portfolio.tenant_id, portfolio.id)
    if account_ids is not None:
        deferred &= account_ids
    return analytics.income(
        session, portfolio.tenant_id, portfolio.id, account_ids=scoped, distribution_account_ids=deferred
    )


@router.get("/{portfolio_id}/accounts", response_model=list[AccountOut])
def get_accounts(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.AccountInfo]:
    # Always lists ALL accounts (this is the selector itself) with per-account valuation.
    accts = analytics.accounts(session, portfolio.tenant_id, portfolio.id)
    # Per-account Day / YTD / LTM rollups (metron-ops#87) — YTD/LTM from each account's
    # reconstructed NAV series, Day legs from the intraday spine (owner build).
    returns = performance.account_period_returns(
        session, portfolio.tenant_id, portfolio.id, today=date.today(), feed_entitled=settings.feed_entitled
    )
    for a in accts:
        r = returns.get(a.account_id)
        if r is None:
            continue
        a.overnight_pct, a.intraday_pct, a.day_pct = r.overnight_pct, r.intraday_pct, r.day_pct
        a.ytd_pct, a.ltm_pct = r.ytd_pct, r.ltm_pct
    return accts


@router.get("/{portfolio_id}/performance", response_model=PerformanceOut)
def get_performance(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> performance.PerformanceSummary:
    """Time-weighted + cumulative return over the recorded NAV snapshot series.

    History builds forward as prices are refreshed; metrics are null until ≥2
    snapshots exist (never a fabricated number). A non-empty ``account_id`` selection
    scopes the series to those accounts' own forward-recorded NAV history (metron-ops#9 —
    per-account NAV can't be reconstructed, so it accrues forward only)."""
    return performance.performance(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.post("/{portfolio_id}/performance/reconstruct", response_model=PerformanceOut)
def reconstruct_performance(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> performance.PerformanceSummary:
    """Seed NAV history from past prices — backfill daily closes (S3 data-spine, yfinance-
    derived upstream) over the ledger span and value the portfolio at each valuation date.
    **Feed-gated (metron-ops#52): 403s for the beta tier (broker-only); NAV history accrues
    forward from broker valuations instead.** Returns the now-populated performance summary."""
    _require_external_market_data(x_preview_feed)
    performance.reconstruct_snapshots(session, portfolio.tenant_id, portfolio.id, today=date.today())
    return performance.performance(session, portfolio.tenant_id, portfolio.id)


class BenchmarkReturnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    label: str
    ret: float | None
    alpha: float | None


class PeriodTileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: str
    label: str
    start_date: date | None
    end_date: date | None
    gain: float | None
    twr: float | None
    benchmarks: list[BenchmarkReturnOut] = []
    note: str | None = None  # honest empty-state reason (e.g. TODAY "as of <prior date>")
    intraday: bool = False  # the live intraday TODAY tile (prior-session close → live NAV)


class PeriodTilesOut(BaseModel):
    tiles: list[PeriodTileOut] = []
    benchmarks_available: bool = False  # benchmark comparison is feed-gated (Pro)
    last_date: date | None = None


@router.get("/{portfolio_id}/performance/tiles", response_model=PeriodTilesOut)
def get_performance_tiles(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> performance.PeriodTilesResult:
    """Overview hero tiles (metron-ops#83): aggregate holdings performance over Today / YTD
    / LTM as $ gain + %TWR, plus per-benchmark return and alpha.

    Benchmark comparison is **feed-gated** (metron-ops#44/#83): the no-feed beta gets
    portfolio-only tiles (``with_benchmarks=False`` — no yfinance-derived index data, per
    metron-ops#52); the owner/Pro feed build gets the SPY/QQQ/IWM columns. Account-scoped
    to the same ``?account_id=`` selection as the rest of the Overview.

    The TODAY tile is a LIVE intraday number (metron-ops#95) when the intraday overlay is in
    effect — its endpoint is the same live NAV the headline TOTAL VALUE shows, valued off
    the same overlay, with benchmark TODAY taken from the Markets-strip index quotes. When
    the overlay is absent (pre-open / stale / no feed) it falls back to the date-guarded
    snapshot path (metron#119)."""
    with_benchmarks = _external_market_data_allowed(x_preview_feed)
    # LIVE intraday endpoint for the TODAY tile: the same overlay + valuation the headline
    # NAV uses (metron-ops#79/#95), so the tile and TOTAL VALUE move together. None when the
    # overlay isn't applied → the snapshot path takes over inside period_tiles.
    live: performance.LiveToday | None = None
    prices, meta = intraday.for_portfolio(
        session, portfolio.tenant_id, portfolio.id,
        feed_entitled=settings.feed_entitled, account_ids=account_ids,
    )
    if meta.applied and prices is not None:
        held = analytics.valued_holdings(
            session, portfolio.tenant_id, portfolio.id, account_ids=account_ids, prices=prices
        )
        priced = [h.market_value for h in held if h.market_value is not None]
        live_nav = sum(priced) if priced else None
        bench: dict[str, tuple[float | None, float | None]] = {}
        if with_benchmarks:
            snap = indices.load_indices()
            if snap.available:
                for q in snap.indices:
                    bench[q.symbol] = (q.last, q.prev_close)
        live = performance.LiveToday(
            nav=live_nav, intraday_applied=True, as_of_utc=meta.as_of_utc, bench=bench
        )
    return performance.period_tiles(
        session,
        portfolio.tenant_id,
        portfolio.id,
        today=date.today(),
        account_ids=account_ids,
        with_benchmarks=with_benchmarks,
        live=live,
    )


class SeriesPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    when: date
    g: float


class AccountSeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    name: str
    points: list[SeriesPointOut] = []
    # "reconstructed" (deep, from lots/ledger) or "forward" (accrues from tracking start).
    coverage: str = "forward"


class BenchmarkSeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    label: str
    points: list[SeriesPointOut] = []


class HoldingsPerfSeriesOut(BaseModel):
    accounts: list[AccountSeriesOut] = []
    benchmarks: list[BenchmarkSeriesOut] = []
    benchmarks_available: bool = False  # benchmark overlays are feed-gated (Pro)


@router.get("/{portfolio_id}/holdings/performance-series", response_model=HoldingsPerfSeriesOut)
def get_holdings_performance_series(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> performance.HoldingsPerfSeries:
    """Per-account performance lines for the Holdings chart (metron-ops#78): one cumulative
    growth index per selected account (all accounts when no ``?account_id=`` selection),
    plus the SPY/QQQ/IWM benchmark overlays.

    Each series is normalized to 1.0 at its first point — the client re-ranges (1M/3M/…/All)
    and re-bases to 100 without a refetch. Benchmark overlays are **feed-gated**
    (metron-ops#52/#78): the no-feed beta gets account lines only (no index overlays)."""
    return performance.account_performance_series(
        session,
        portfolio.tenant_id,
        portfolio.id,
        today=date.today(),
        account_ids=account_ids,
        with_benchmarks=_external_market_data_allowed(x_preview_feed),
    )


class IntradayLegDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    when: date
    overnight_pct: float | None = None
    intraday_pct: float | None = None
    day_pct: float | None = None


class IntradayLegHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    days: list[IntradayLegDayOut] = []
    cum_overnight_pct: float | None = None
    cum_intraday_pct: float | None = None
    cum_day_pct: float | None = None
    n_days: int = 0


@router.get("/{portfolio_id}/intraday-legs", response_model=IntradayLegHistoryOut)
def get_intraday_legs(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> performance.IntradayLegHistory:
    """The recorded overnight/intraday/day decomposition history + the cumulative
    (compounded) split — how much of the portfolio's drift arrives overnight vs intraday
    (metron-ops#87). Accrues forward from when recording began (the spine keeps only the
    latest snapshot, so it can't be reconstructed); empty until the first daily record."""
    return performance.intraday_leg_history(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/tax", response_model=TaxOut)
def get_tax(
    taxable_only: bool = True,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> tax.TaxSummary:
    """Per-lot tax view — holding-period term, unrealized P&L (in the base currency) at
    the latest cached close, and harvestable losses. Defaults to **taxable accounts
    only** (gains in an IRA/401(k)/Roth are never taxed); pass ``taxable_only=false`` to
    include tax-advantaged accounts. A ``?account_id=`` selection is intersected with the
    taxable set (taxable-only always wins). Unrealized totals are null until prices are
    cached (refresh prices first); cost basis + term show regardless."""
    return tax.tax_lots(
        session, portfolio.tenant_id, portfolio.id, today=date.today(),
        taxable_only=taxable_only, selected_account_ids=account_ids,
    )


class TearsheetPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    currency: str = "USD"
    quantity: float
    avg_cost: float
    cost_basis: float | None
    market_value: float | None
    unrealized_gain: float | None
    unrealized_pct: float | None
    weight_pct: float | None
    accounts: list[str] = []


class TearsheetPerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    return_vs_cost: float | None
    period_returns: dict[str, float] = {}
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    beta_vs_spy: float | None = None
    vs_spy: float | None = None
    n_bars: int = 0
    history_from: date | None = None


class TearsheetTechnicalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rsi_14: float | None = None
    pct_from_52wk_high: float | None = None
    forward_div_yield: float | None = None


class TickerFundamentalsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    yf_symbol: str
    sector: str | None
    industry: str | None
    market_cap: float | None
    beta: float | None
    trailing_pe: float | None
    forward_pe: float | None
    peg: float | None
    ev_ebitda: float | None
    earnings_growth: float | None
    revenue_growth: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    quick_ratio: float | None
    roe: float | None
    roa: float | None
    gross_margins: float | None
    operating_margins: float | None
    dividend_yield: float | None


class CompOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    sector: str | None
    trailing_pe: float | None
    forward_pe: float | None
    ev_ebitda: float | None
    debt_to_equity: float | None
    dividend_yield: float | None
    is_self: bool = False


class TearsheetConsensusOut(BaseModel):
    """Consensus research + news sentiment tearsheet panel (metron-ops#105). Free-source
    spine, feed-gated; the paid forward-estimate columns resolve N/A until metron-ops#107."""
    model_config = ConfigDict(from_attributes=True)

    consensus_rating: str | None = None
    consensus_score: float | None = None
    price_target_mean: float | None = None
    price_target_median: float | None = None
    price_target_upside: float | None = None
    num_analysts: int | None = None
    news_sentiment: float | None = None
    news_articles: int | None = None
    news_as_of: date | None = None
    estimates_available: bool = False
    estimates_reason: str = ""
    forward_eps: float | None = None
    forward_revenue: float | None = None
    forward_pe_consensus: float | None = None
    peg_consensus: float | None = None
    estimate_revision_trend: float | None = None


class TearsheetAttractivenessComponentOut(BaseModel):
    """One inspectable line of the attractiveness breakdown — the gauge tooltip renders these
    so the weighting is never a black box (metron-ops#106)."""
    model_config = ConfigDict(from_attributes=True)

    key: str
    weight: float
    sub_score: float


class TearsheetAttractivenessOut(BaseModel):
    """Composite attractiveness gauge (metron-ops#106, Phase 2) — the 0–100 headline score plus
    its per-component breakdown. ``available`` is false off-feed or on a total coverage gap."""
    model_config = ConfigDict(from_attributes=True)

    available: bool = False
    score: float | None = None
    coverage: int | None = None
    components: list[TearsheetAttractivenessComponentOut] = []


class TearsheetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    base_currency: str = "USD"
    as_of: date
    position: TearsheetPositionOut
    performance: TearsheetPerformanceOut
    technical: TearsheetTechnicalOut
    fundamentals_available: bool = False
    fundamentals_reason: str = ""
    fundamentals: TickerFundamentalsOut | None = None
    fundamentals_as_of: date | None = None
    comps: list[CompOut] = []
    consensus_available: bool = False
    consensus_as_of: date | None = None
    consensus: TearsheetConsensusOut = TearsheetConsensusOut()
    attractiveness: TearsheetAttractivenessOut = TearsheetAttractivenessOut()


@router.get("/{portfolio_id}/tearsheet/{ticker}", response_model=TearsheetOut)
def get_tearsheet(
    ticker: str,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> tearsheet_service.Tearsheet:
    """IB-style per-holding tearsheet (metron-ops#22): Position + Performance + Technical
    from data Metron already has; the valuation-multiples / balance-sheet / comps blocks
    come from the feed-gated fundamentals spine artifact (yfinance-derived → Pro) and
    populate only on a feed-entitled build. 404 if the portfolio doesn't hold the ticker."""
    sheet = tearsheet_service.tearsheet(
        session, portfolio.tenant_id, portfolio.id, ticker.upper(),
        feed_enabled=_external_market_data_allowed(x_preview_feed),
    )
    if sheet is None:
        raise HTTPException(status_code=404, detail=f"{ticker.upper()} is not a current holding.")
    return sheet


@router.get("/{portfolio_id}/risk", response_model=RiskOut)
def get_risk(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> risk.RiskSummary:
    """Factor risk decomposition + tracking error vs SPY, from already-cached price
    history. Marked not-computable (with a reason) when the cache lacks enough
    history — POST .../risk/compute to backfill it. ``?account_id=`` scopes the holdings.

    Feed-dependent: when the active tier / data feed excludes it the matrix is enforced
    here — returns ``computable=false`` with the entitlement ``reason`` + ``required_tier``
    instead of computing (Risk needs a licensed price feed)."""
    feat = _effective_entitlement("risk", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return risk.RiskSummary(computable=False, reason=feat["reason"], required_tier=feat["required_tier"])
    return risk.compute_risk(
        session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=False, account_ids=account_ids
    )


@router.post("/{portfolio_id}/risk/compute", response_model=RiskOut)
def compute_risk(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> risk.RiskSummary:
    """Backfill the held + factor-ETF price history over the risk window, then compute
    the factor risk decomposition. The heavier (network) path behind the GET.
    ``?account_id=`` scopes the holdings. Feed-dependent — gated by the entitlement
    matrix (see GET .../risk)."""
    feat = _effective_entitlement("risk", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return risk.RiskSummary(computable=False, reason=feat["reason"], required_tier=feat["required_tier"])
    return risk.compute_risk(
        session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=True, account_ids=account_ids
    )


@router.get("/{portfolio_id}/attribution", response_model=AttributionOut)
def get_attribution(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> attribution.AttributionSummary:
    """Brinson-Fachler sector attribution vs SPY, from already-cached prices + sectors.
    Marked not-computable (with a reason) when the cache lacks history or holding
    sectors — POST .../attribution/compute to source them. ``?account_id=`` scopes the
    holdings. Feed-dependent — gated by the entitlement matrix (returns
    ``computable=false`` + ``required_tier`` when the active tier / feed excludes it)."""
    feat = _effective_entitlement("attribution", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return attribution.AttributionSummary(
            computable=False, reason=feat["reason"], required_tier=feat["required_tier"]
        )
    return attribution.compute_attribution(
        session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=False, account_ids=account_ids
    )


@router.post("/{portfolio_id}/attribution/compute", response_model=AttributionOut)
def compute_attribution(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> attribution.AttributionSummary:
    """Resolve holding sectors + backfill held and SPDR-ETF history over the window,
    then run the attribution. The heavier (network) path behind the GET. ``?account_id=``
    scopes the holdings. Feed-dependent — gated by the entitlement matrix (see GET
    .../attribution)."""
    feat = _effective_entitlement("attribution", x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return attribution.AttributionSummary(
            computable=False, reason=feat["reason"], required_tier=feat["required_tier"]
        )
    return attribution.compute_attribution(
        session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=True, account_ids=account_ids
    )


@router.get("/{portfolio_id}/calendar", response_model=CalendarOut)
def get_calendar(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> calendar.CalendarSummary:
    """Upcoming held-ticker earnings within the horizon, from cached dates. POST
    .../calendar/refresh to populate/refresh the cache (the network path)."""
    return calendar.upcoming_events(session, portfolio.tenant_id, portfolio.id, today=date.today())


@router.post("/{portfolio_id}/calendar/refresh", response_model=CalendarOut)
def refresh_calendar(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_feed: str | None = Header(default=None),
) -> calendar.CalendarSummary:
    """Refresh each held ticker's next earnings date (S3 data-spine, yfinance-derived
    upstream), then return the upcoming-events calendar. The heavier (network) path behind
    the GET. **Feed-gated (metron-ops#52): 403s for the beta tier (broker-only)."""
    _require_external_market_data(x_preview_feed)
    tickers = [h.ticker for h in analytics.holdings(session, portfolio.tenant_id, portfolio.id)]
    calendar.refresh_earnings(session, tickers)
    return calendar.upcoming_events(session, portfolio.tenant_id, portfolio.id, today=date.today())


@router.put("/{portfolio_id}/securities/{symbol}/label", response_model=SecurityLabelOut)
def set_security_label(
    symbol: str,
    body: SecurityLabelIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SecurityLabelOut:
    """Set (or clear, with an empty label) a user alias for a symbol so a numeric-CUSIP
    bond is legible (metron-ops#47). Tenant-scoped; the alias survives re-imports."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=422, detail="symbol is required")
    stored = labels.set_label(session, portfolio.tenant_id, sym, body.label)
    return SecurityLabelOut(symbol=sym, label=stored)


@router.put(
    "/{portfolio_id}/securities/{symbol}/classification",
    response_model=SecurityClassificationOut,
)
def set_security_classification(
    symbol: str,
    body: SecurityClassificationIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> SecurityClassificationOut:
    """Set (or clear) a tenant's GICS-sector / country-of-domicile override for a symbol so
    an Unclassified holding can be placed in the Allocation breakdown. Only the fields
    present in the body are changed; an explicit null/empty clears that field, and clearing
    both removes the override (reverting to the spine-resolved value). Tenant-scoped — never
    mutates the shared securities reference row."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=422, detail="symbol is required")
    fields = body.model_fields_set
    if "instrument_type" in fields and body.instrument_type and body.instrument_type not in _INSTRUMENT_TYPES:
        raise HTTPException(status_code=422, detail="Unknown instrument type")
    stored = classifications_service.set_classification(
        session,
        portfolio.tenant_id,
        sym,
        sector=body.sector if "sector" in fields else classifications_service.UNSET,
        country=body.country if "country" in fields else classifications_service.UNSET,
        instrument_type=body.instrument_type if "instrument_type" in fields else classifications_service.UNSET,
    )
    return SecurityClassificationOut(
        symbol=sym,
        sector=stored.sector if stored else None,
        country=stored.country if stored else None,
        instrument_type=stored.instrument_type if stored else None,
    )


@router.get("/{portfolio_id}/watchlist", response_model=list[WatchlistEntryOut])
def get_watchlist(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[watchlist.WatchlistEntry]:
    """The portfolio's watchlist — tracked tickers (held or not) with reference data
    (name / sector / next earnings) + a held flag. Read-only/illustrative in the no-feed
    beta: no live price, since un-held tickers have no price source (metron-ops#42)."""
    return watchlist.list_watchlist(session, portfolio.tenant_id, portfolio.id)


@router.post("/{portfolio_id}/watchlist", response_model=WatchlistEntryOut, status_code=201)
def add_watchlist(
    body: WatchlistIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> WatchlistEntryOut:
    """Add a symbol to the watchlist (idempotent; re-add updates the note). Caches a
    Security row so reference data can resolve."""
    symbol = (body.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    watchlist.add_to_watchlist(session, portfolio.tenant_id, portfolio.id, symbol, note=body.note)
    # Return the enriched entry (held flag + reference data), not the bare row.
    entry = next(
        (e for e in watchlist.list_watchlist(session, portfolio.tenant_id, portfolio.id) if e.symbol == symbol),
        None,
    )
    if entry is None:  # pragma: no cover - just-added row must be present
        raise HTTPException(status_code=500, detail="watchlist add did not persist")
    return WatchlistEntryOut.model_validate(entry)


@router.delete("/{portfolio_id}/watchlist/{symbol}", response_model=WatchlistDeleteOut)
def remove_watchlist(
    symbol: str,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> WatchlistDeleteOut:
    """Remove a symbol from the watchlist (404 if it isn't on it)."""
    removed = watchlist.remove_from_watchlist(session, portfolio.tenant_id, portfolio.id, symbol)
    if not removed:
        raise HTTPException(status_code=404, detail="Symbol not on the watchlist")
    return WatchlistDeleteOut(symbol=symbol.strip().upper(), removed=True)


# ── Crypto (standalone wallet-address tracking; metron-ops#111) ──────────────────────────


class CryptoAddressIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain: str            # "BTC" | "ETH"
    address: str
    label: str | None = None


class CryptoPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chain: str
    address: str
    label: str | None = None
    symbol: str | None = None
    balance: float | None = None
    price_usd: float | None = None
    value_usd: float | None = None
    synced: bool


class CryptoSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    available: bool
    as_of_utc: str | None = None
    stale: bool = False
    total_usd: float | None = None
    n_pending: int = 0
    positions: list[CryptoPositionOut] = []
    reason: str | None = None


class CryptoAddressDeleteOut(BaseModel):
    id: uuid.UUID
    removed: bool


@router.get("/{portfolio_id}/crypto", response_model=CryptoSummaryOut)
def get_crypto(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> crypto.CryptoSummary:
    """The portfolio's tracked crypto wallets joined with the producer's synced balances
    (metron-ops#111). Standalone — decoupled from the EOD-close holdings/NAV. Addresses
    awaiting a first sync render as ``synced=False`` (never zeroed). Forward-records today's
    total value (idempotent) when a fresh total is present."""
    summary = crypto.for_portfolio(session, portfolio.tenant_id, portfolio.id)
    crypto.record_snapshot(session, portfolio.tenant_id, portfolio.id, summary)
    return summary


@router.post("/{portfolio_id}/crypto/addresses", response_model=CryptoPositionOut, status_code=201)
def add_crypto_address(
    body: CryptoAddressIn,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> CryptoPositionOut:
    """Add a wallet to track (idempotent on chain+address; re-add updates the label). The
    address is format-validated for its chain (422 on a bad address). Publishes the updated
    fetch universe so the producer picks the wallet up on its next cycle."""
    try:
        row = crypto.add_address(
            session, portfolio.tenant_id, portfolio.id, body.chain, body.address, label=body.label
        )
    except crypto.InvalidAddress as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # The freshly added address has no synced balance yet — return it as a pending position.
    return CryptoPositionOut(
        id=row.id, chain=row.chain, address=row.address, label=row.label,
        symbol=None, balance=None, price_usd=None, value_usd=None, synced=False,
    )


@router.delete("/{portfolio_id}/crypto/addresses/{address_id}", response_model=CryptoAddressDeleteOut)
def delete_crypto_address(
    address_id: uuid.UUID,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> CryptoAddressDeleteOut:
    """Stop tracking a wallet (404 if it isn't this portfolio's). Re-publishes the fetch
    universe so the producer drops it."""
    removed = crypto.delete_address(session, portfolio.tenant_id, portfolio.id, address_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Wallet address not found")
    return CryptoAddressDeleteOut(id=address_id, removed=True)


@router.get("/{portfolio_id}/accounts/{account_id}", response_model=AccountDetailOut)
def get_account_detail(
    account: models.Account = Depends(_owned_account),
    session: Session = Depends(get_session),
) -> AccountDetailOut:
    """One account's holdings + realized lots + transactions, all scoped to that account.

    The per-account drill-down for a multi-account portfolio. Holdings are valued from
    the cached close when available (cost-basis-only otherwise — never fabricated)."""
    return AccountDetailOut(
        account=AccountOut(
            account_id=account.id,
            broker=account.broker,
            external_id=account.external_id,
            name=account.name or "",
            currency=account.currency,
            nickname=account.nickname,
            institution=account.institution,
            account_type=account.account_type,
            tax_treatment=account.tax_treatment,
            taxable=account_meta.is_taxable(account),
        ),
        holdings=security_perf.enrich_holdings(
            session, account.tenant_id, account.portfolio_id,
            analytics.valued_holdings(session, account.tenant_id, account.portfolio_id, account.id),
            as_of=date.today(), feed_entitled=settings.feed_entitled, account_ids={account.id},
        ),
        realized=analytics.realized(session, account.tenant_id, account.portfolio_id, account.id),
        transactions=analytics.transactions(session, account.tenant_id, account.portfolio_id, account.id),
    )


@router.get("/{portfolio_id}/summary", response_model=SummaryOut)
def get_summary(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> analytics.PortfolioSummary:
    """Portfolio home totals — cost basis, realized, income, plus market value /
    unrealized when prices are cached. ``?account_id=`` scopes every total to the
    selected accounts (absent = whole portfolio). The headline market value (NAV)
    recomputes from the LIVE intraday balances on a feed-entitled build (metron-ops#79)."""
    prices, _ = intraday.for_portfolio(
        session, portfolio.tenant_id, portfolio.id, feed_entitled=settings.feed_entitled, account_ids=account_ids
    )
    return analytics.summary(
        session, portfolio.tenant_id, portfolio.id, account_ids=account_ids, prices=prices
    )


@router.get("/{portfolio_id}/intraday", response_model=IntradayStatusOut)
def get_intraday_status(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> IntradayStatusOut:
    """Live-valuation status for the portfolio (metron-ops#79): whether the intraday
    overlay is currently applied, its freshness (``as_of_utc`` / ``stale``), and how many
    held positions got a fresh quote. Drives the "intraday · ~15-min delayed · as of HH:MM"
    label and the client poll. The poll hits ``_owned_portfolio``, which touches the
    data-spine UI heartbeat — so an open Metron keeps the intraday producer publishing."""
    _, m = intraday.for_portfolio(
        session, portfolio.tenant_id, portfolio.id, feed_entitled=settings.feed_entitled, account_ids=account_ids
    )
    return IntradayStatusOut(
        applied=m.applied, as_of_utc=m.as_of_utc, stale=m.stale, n_priced=m.n_priced, reason=m.reason
    )


@router.get("/{portfolio_id}/today", response_model=TodayOut)
def get_today(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> intraday.TodaySummary:
    """The Today view (metron-ops#23): per-holding prior-close / open / latest with the
    overnight·intraday·day P&L decomposition + portfolio totals, from the intraday spine
    quotes. Feed-gated; outside market hours the snapshot is ``stale`` and the rows read
    "as of close". ``?account_id=`` scopes the holdings like every other page. The request
    touches the data-spine UI heartbeat (via ``_owned_portfolio``) so the producer keeps
    publishing while the page is open."""
    return intraday.today_view(
        session, portfolio.tenant_id, portfolio.id,
        feed_entitled=settings.feed_entitled, account_ids=account_ids,
    )
