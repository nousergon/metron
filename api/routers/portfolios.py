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
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.db.session import get_session
from api.services import account_meta, analytics, attribution, calendar, data_spine, performance, persistence, risk, tax
from api.services import fx as fx_service
from api.services import prices as price_service
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


class PreferencesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    risk_tolerance: str | None = None
    objective: str | None = None
    notes: str | None = None


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
    market_value_local: float | None = None
    cost_basis_base: float | None = None
    market_value: float | None = None
    unrealized_gain: float | None = None
    unrealized_pct: float | None = None


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


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    base_currency: str
    n_accounts: int
    n_holdings: int
    total_cost_basis: float
    realized_st: float
    realized_lt: float
    realized_total: float
    dividends: float
    interest: float
    taxable_income: float
    market_value: float | None = None
    unrealized_gain: float | None = None
    n_unconverted: int = 0


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
    points: list[PerfPointOut]


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
    harvestable_loss: float | None
    n_accounts_excluded: int = 0
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
    ``market_data_sync_enabled``). When the **tier simulator** is on (owner-only,
    never on the public product) the ``X-Preview-Tier`` / ``X-Preview-Feed`` headers
    override them — mirroring ``GET /meta/entitlements`` so a feed toggle in the
    simulator is honored server-side too. A bad preview tier falls back to the
    deployment default rather than 500-ing the compute call.

    Returns the per-feature dict from ``entitlements.resolve`` (``available`` /
    ``reason`` / ``required_tier`` / …) so callers can short-circuit a feed-dependent
    endpoint into an honest not-computable response when the tier excludes it.
    """
    tier = settings.default_tier
    feed = settings.market_data_sync_enabled
    if settings.tier_simulator:
        if x_preview_tier is not None:
            tier = x_preview_tier
        if x_preview_feed is not None:
            feed = x_preview_feed.strip().lower() == "true"
    try:
        resolved = ent.resolve(tier, feed_enabled=feed)
    except ValueError:
        resolved = ent.resolve(settings.default_tier, feed_enabled=settings.market_data_sync_enabled)
    return next(f for f in resolved["features"] if f["key"] == feature_key)


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
    # is actually looking. Every authenticated portfolio request flows through this
    # dependency, making it the one natural chokepoint for "Metron is open".
    data_spine.touch_ui_heartbeat()
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
    connector = IbkrFlexConnector(body.token, body.query_id, persist_bronze=False)
    snapshot = connector.sync()
    if snapshot.error:
        raise HTTPException(status_code=502, detail=f"IBKR Flex fetch failed: {snapshot.error}")
    persisted = persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=snapshot
    )
    return _summarize(snapshot, persisted, parsed=len(snapshot.activities), skipped=0, errors=[])


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
    return SnapTradeConnectionsOut(connections=out)


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
) -> PriceRefreshOut:
    """Refresh the EOD price cache for this portfolio's currently-held tickers.

    Fetches the latest close per held ticker (yfinance, personal tier) into the global
    ``price_bars`` cache, after which holdings/summary report market value + unrealized
    P&L. Idempotent — re-running updates the same session's bar. Tickers the source
    can't price are skipped (they stay cost-basis-only)."""
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
    session: Session = Depends(get_session),
) -> list[analytics.Holding]:
    # Valued when a cached close exists for the ticker; cost-basis-only otherwise.
    # ``?account_id=`` (repeatable) scopes to the selected accounts; absent = all.
    return analytics.valued_holdings(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionOut])
def get_transactions(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list[analytics.TransactionRow]:
    return analytics.transactions(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/realized", response_model=list[RealizedOut])
def get_realized(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list[analytics.RealizedLot]:
    return analytics.realized(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/income", response_model=list[IncomeOut])
def get_income(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    account_ids: set[uuid.UUID] | None = Depends(_selected_account_ids),
    session: Session = Depends(get_session),
) -> list:
    """Per-year realized income (cap gains ST/LT + dividends + interest), newest first."""
    return analytics.income(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)


@router.get("/{portfolio_id}/accounts", response_model=list[AccountOut])
def get_accounts(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.AccountInfo]:
    # Always lists ALL accounts (this is the selector itself) with per-account valuation.
    return analytics.accounts(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/performance", response_model=PerformanceOut)
def get_performance(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> performance.PerformanceSummary:
    """Time-weighted + cumulative return over the recorded NAV snapshot series.

    History builds forward as prices are refreshed; metrics are null until ≥2
    snapshots exist (never a fabricated number)."""
    return performance.performance(session, portfolio.tenant_id, portfolio.id)


@router.post("/{portfolio_id}/performance/reconstruct", response_model=PerformanceOut)
def reconstruct_performance(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> performance.PerformanceSummary:
    """Seed NAV history from past prices — backfill daily closes over the ledger span
    and value the portfolio at each valuation date. Gives instant multi-year
    performance instead of waiting for forward-recording to accumulate. Returns the
    now-populated performance summary."""
    performance.reconstruct_snapshots(session, portfolio.tenant_id, portfolio.id, today=date.today())
    return performance.performance(session, portfolio.tenant_id, portfolio.id)


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
) -> calendar.CalendarSummary:
    """Refresh each held ticker's next earnings date (yfinance), then return the
    upcoming-events calendar. The heavier (network) path behind the GET."""
    tickers = [h.ticker for h in analytics.holdings(session, portfolio.tenant_id, portfolio.id)]
    calendar.refresh_earnings(session, tickers)
    return calendar.upcoming_events(session, portfolio.tenant_id, portfolio.id, today=date.today())


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
        holdings=analytics.valued_holdings(session, account.tenant_id, account.portfolio_id, account.id),
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
    selected accounts (absent = whole portfolio)."""
    return analytics.summary(session, portfolio.tenant_id, portfolio.id, account_ids=account_ids)
