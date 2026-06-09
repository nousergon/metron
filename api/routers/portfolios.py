"""Portfolios router — CRUD + file ingestion + ledger-derived analytics (PH1).

Tenant resolution is stubbed (``X-Tenant-Id`` header) until auth lands in PH4; the
real product derives the tenant from the authenticated session.

PH1 adds the ingestion round-trip the free beta is built on: ``POST …/import/csv``
and ``POST …/import/ofx`` land a broker CSV or OFX/QFX download through the same
canonical pipeline + persistence bridge, and ``GET …/holdings`` / ``…/transactions``
/ ``…/realized`` read the ledger-derived view back out. Price-dependent analytics
(market value, performance, risk) arrive in later PH1–PH3 increments.
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
    errors: list[SkipOut]


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


def _persist_and_summarize(
    session: Session, portfolio: models.Portfolio, result: FileImportResult
) -> ImportOut:
    """Persist a parsed file through the shared bridge and build the import summary.

    One path for every file type — securities upsert, accounts upsert, transactions
    unioned by source_key (idempotent re-upload)."""
    persisted = persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=result.snapshot
    )
    return ImportOut(
        source=result.snapshot.source,
        rows_parsed=result.parsed,
        rows_skipped=result.skipped,
        accounts_created=persisted.accounts_created,
        securities_created=persisted.securities_created,
        transactions_inserted=persisted.transactions_inserted,
        transactions_skipped=persisted.transactions_skipped,
        errors=[SkipOut(ref=e.ref, reason=e.reason) for e in result.errors[:_MAX_ERROR_DETAIL]],
    )


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
