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
from api.services import analytics, persistence
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


class AccountDetailOut(BaseModel):
    """One account's holdings + activity — the per-account drill-down, in one call.

    Every figure is scoped to this single account (its own ledger + broker-reported
    positions), so a multi-account portfolio breaks down cleanly. Price-free, like the
    portfolio-level views."""

    account: AccountOut
    holdings: list[HoldingOut]
    realized: list[RealizedOut]
    transactions: list[TransactionOut]


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


@router.get("/{portfolio_id}/holdings", response_model=list[HoldingOut])
def get_holdings(
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> list[analytics.Holding]:
    return analytics.holdings(session, portfolio.tenant_id, portfolio.id)


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


@router.get("/{portfolio_id}/accounts/{account_id}", response_model=AccountDetailOut)
def get_account_detail(
    account: models.Account = Depends(_owned_account),
    session: Session = Depends(get_session),
) -> AccountDetailOut:
    """One account's holdings + realized lots + transactions, all scoped to that account.

    The per-account drill-down for a multi-account portfolio. Price-free (cost basis,
    realized, ledger) — no market value until a licensed price feed lands."""
    return AccountDetailOut(
        account=AccountOut(
            account_id=account.id,
            broker=account.broker,
            external_id=account.external_id,
            name=account.name or "",
            currency=account.currency,
        ),
        holdings=analytics.holdings(session, account.tenant_id, account.portfolio_id, account.id),
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
