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

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.db.session import get_session
from api.services import analytics, performance, persistence, risk, tax
from api.services import prices as price_service
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.broker_io.file_import import FileImportError, FileImportResult
from portfolio_analytics.broker_io.ofx_import import parse_ofx
from portfolio_analytics.ingestion.ibkr_flex_connector import IbkrFlexConnector

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


class HoldingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    quantity: float
    avg_cost: float
    cost_basis: float
    # Null on the price-free path; populated from the cached close (see prices/refresh).
    last_price: float | None = None
    last_price_date: date | None = None
    market_value: float | None = None
    unrealized_gain: float | None = None
    unrealized_pct: float | None = None


class RealizedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    open_date: date
    close_date: date
    quantity: float
    proceeds: float
    cost_basis: float
    gain: float
    long_term: bool


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
    points: list[PerfPointOut]


class TaxLotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    open_date: date
    quantity: float
    cost_basis: float
    term: str
    market_value: float | None
    unrealized_gain: float | None
    harvestable_loss: float


class TaxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    as_of: date
    n_lots: int
    n_priced: int
    unrealized_st: float | None
    unrealized_lt: float | None
    unrealized_total: float | None
    harvestable_loss: float
    lots: list[TaxLotOut]


class RiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    computable: bool
    benchmark: str
    reason: str | None
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
    return portfolio


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
    # Record today's NAV snapshot off the freshly-cached prices (the forward-recorded
    # performance series). None when nothing could be priced.
    snap = performance.record_snapshot(session, portfolio.tenant_id, portfolio.id, today=date.today())
    return PriceRefreshOut(symbols_requested=len(symbols), prices_updated=updated, snapshot_recorded=snap is not None)


@router.get("/{portfolio_id}/holdings", response_model=list[HoldingOut])
def get_holdings(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.Holding]:
    # Valued when a cached close exists for the ticker; cost-basis-only otherwise.
    return analytics.valued_holdings(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/transactions", response_model=list[TransactionOut])
def get_transactions(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.TransactionRow]:
    return analytics.transactions(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/realized", response_model=list[RealizedOut])
def get_realized(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.RealizedLot]:
    return analytics.realized(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/income", response_model=list[IncomeOut])
def get_income(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list:
    """Per-year realized income (cap gains ST/LT + dividends + interest), newest first."""
    return analytics.income(session, portfolio.tenant_id, portfolio.id)


@router.get("/{portfolio_id}/accounts", response_model=list[AccountOut])
def get_accounts(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.AccountInfo]:
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
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> tax.TaxSummary:
    """Per-lot tax view — holding-period term, unrealized P&L at the latest cached
    close, and harvestable losses. Unrealized totals are null until prices are cached
    (refresh prices first); cost basis + term show regardless."""
    return tax.tax_lots(session, portfolio.tenant_id, portfolio.id, today=date.today())


@router.get("/{portfolio_id}/risk", response_model=RiskOut)
def get_risk(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> risk.RiskSummary:
    """Factor risk decomposition + tracking error vs SPY, from already-cached price
    history. Marked not-computable (with a reason) when the cache lacks enough
    history — POST .../risk/compute to backfill it."""
    return risk.compute_risk(session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=False)


@router.post("/{portfolio_id}/risk/compute", response_model=RiskOut)
def compute_risk(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> risk.RiskSummary:
    """Backfill the held + factor-ETF price history over the risk window, then compute
    the factor risk decomposition. The heavier (network) path behind the GET."""
    return risk.compute_risk(session, portfolio.tenant_id, portfolio.id, today=date.today(), do_backfill=True)


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
        ),
        holdings=analytics.valued_holdings(session, account.tenant_id, account.portfolio_id, account.id),
        realized=analytics.realized(session, account.tenant_id, account.portfolio_id, account.id),
        transactions=analytics.transactions(session, account.tenant_id, account.portfolio_id, account.id),
    )


@router.get("/{portfolio_id}/summary", response_model=SummaryOut)
def get_summary(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> analytics.PortfolioSummary:
    """Portfolio home totals — all price-free (cost basis, realized, income). No market
    value / unrealized P&L until a licensed price feed lands."""
    return analytics.summary(session, portfolio.tenant_id, portfolio.id)
