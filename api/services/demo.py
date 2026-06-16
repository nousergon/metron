"""Demo / sample portfolio — a canned, frozen, READ-ONLY fixture a prospect can open
with no signup and no brokerage connection (metron-ops#42).

It lives under a fixed well-known tenant + portfolio id so every existing read endpoint
serves it unchanged (the web demo entry resolves the demo tenant without auth). Seeded
idempotently at startup by replaying a frozen transactions CSV through the SAME import
bridge a real upload uses — so the demo exercises the real ingestion/valuation path, not
a parallel mock. Prices + NAV history are seeded directly (frozen) so every page renders
(holdings value, Performance has metrics, Tax has a realized lot) without a live refresh.

It deliberately spans multiple accounts (taxable + tax-deferred) and asset classes
(equity / ETF / bond / cash) so the tax-status grouping (#46) and security-type grouping
(#47) both show. Writes to the demo tenant are refused (``assert_writable``) so the
fixture can never be mutated by a visitor.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import persistence
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv

# Fixed, well-known ids (stable across restarts so links don't break). The tenant id
# spells "demo" in its tail; never issued to a real user (auth mints random UUIDs).
DEMO_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000de60")
DEMO_PORTFOLIO_ID = uuid.UUID("00000000-0000-0000-0000-00000000de61")

# Frozen transactions — two accounts, four asset classes, a partial sell (realized lot)
# and dividends. Dates span ~6 months so Performance annualizes (≥30-day window).
_DEMO_CSV = """date,type,symbol,quantity,price,amount,account
2024-01-08,BUY,AAPL,40,185,7400,Demo Brokerage
2024-01-08,BUY,VOO,15,440,6600,Demo Brokerage
2024-01-16,BUY,MSFT,20,390,7800,Demo IRA
2024-02-02,BUY,912828YK0,50,98,4900,Demo IRA
2024-02-20,DIVIDEND,AAPL,0,0,24,Demo Brokerage
2024-03-15,BUY,VMFXX,2000,1,2000,Demo Brokerage
2024-05-10,SELL,AAPL,15,205,3075,Demo Brokerage
2024-06-03,DIVIDEND,VOO,0,0,38,Demo Brokerage
"""

# Per-symbol reference metadata applied after import (the CSV path defaults everything to
# equity): name + the asset_class that drives security-type grouping (#47).
_SECURITY_META: dict[str, tuple[str, str]] = {
    "AAPL": ("Apple Inc.", "equity"),
    "MSFT": ("Microsoft Corp.", "equity"),
    "VOO": ("Vanguard S&P 500 ETF", "etf"),
    "912828YK0": ("US Treasury Note 4.0% 2026", "bond"),
    "VMFXX": ("Vanguard Federal Money Market", "cash"),
}

# Frozen EOD closes (as-of date) so holdings value without a live price refresh.
_PRICE_AS_OF = date(2024, 6, 28)
_DEMO_PRICES: dict[str, float] = {
    "AAPL": 210.0,
    "MSFT": 450.0,
    "VOO": 490.0,
    "912828YK0": 99.0,
    "VMFXX": 1.0,
}

# Frozen NAV history (portfolio-level) so Performance has metrics. spy_close drives the
# benchmark/alpha; the ≥30-day span lets the annualized figures show (per the #44 guard).
_DEMO_NAV: list[tuple[date, float, float, float]] = [
    # (snap_date, nav, cost_basis, spy_close)
    (date(2024, 1, 16), 25000.0, 25000.0, 478.0),
    (date(2024, 3, 28), 26500.0, 25700.0, 505.0),
    (date(2024, 6, 28), 28550.0, 25925.0, 545.0),
]

# Account tax treatment — the IRA is tax-deferred so the tax-status grouping (#46) shows
# both a taxable and a tax-advantaged bucket. (Demo Brokerage derives to Taxable.)
_ACCOUNT_TAX: dict[str, tuple[str, str | None]] = {
    # external_id -> (tax_treatment, account_type)
    "Demo IRA": ("tax_deferred", "IRA"),
}


def is_demo_tenant(tenant_id: uuid.UUID) -> bool:
    return tenant_id == DEMO_TENANT_ID


def assert_writable(tenant_id: uuid.UUID) -> None:
    """Refuse any write to the demo tenant — the sample portfolio is read-only so a
    visitor can never mutate the shared fixture. Imports, edits, deletes, price refresh
    and reconstruction all flow through this guard. Raises 403 for the demo tenant."""
    if is_demo_tenant(tenant_id):
        # Local import to avoid a module cycle (routers import this module).
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="The demo portfolio is read-only.")


def ensure_demo_seeded(session: Session) -> bool:
    """Idempotently seed the demo tenant/portfolio. Returns True if it seeded this call,
    False if it already existed. Safe to call on every startup."""
    if session.get(models.Portfolio, DEMO_PORTFOLIO_ID) is not None:
        return False

    if session.get(models.Tenant, DEMO_TENANT_ID) is None:
        session.add(models.Tenant(id=DEMO_TENANT_ID, name="Demo"))
    session.add(
        models.Portfolio(
            id=DEMO_PORTFOLIO_ID, tenant_id=DEMO_TENANT_ID, name="Demo portfolio", base_currency="USD"
        )
    )
    session.flush()

    # Replay the frozen CSV through the real import bridge (securities + accounts + ledger).
    result = parse_transactions_csv(_DEMO_CSV)
    persistence.persist_snapshot(
        session, tenant_id=DEMO_TENANT_ID, portfolio_id=DEMO_PORTFOLIO_ID, snapshot=result.snapshot
    )

    _apply_security_meta(session)
    _apply_account_tax(session)
    _seed_prices(session)
    _seed_nav(session)
    session.commit()
    return True


def _apply_security_meta(session: Session) -> None:
    """Set name + asset_class on the demo's securities (the CSV path leaves them equity)."""
    rows = session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(_SECURITY_META)))
    ).all()
    for sec in rows:
        meta = _SECURITY_META.get(sec.symbol)
        if meta:
            sec.name, sec.asset_class = meta


def _apply_account_tax(session: Session) -> None:
    rows = session.scalars(
        select(models.Account).where(models.Account.portfolio_id == DEMO_PORTFOLIO_ID)
    ).all()
    for acct in rows:
        tax = _ACCOUNT_TAX.get(acct.external_id)
        if tax:
            acct.tax_treatment, acct.account_type = tax


def _seed_prices(session: Session) -> None:
    """Frozen EOD close per held symbol so holdings show a market value (no live fetch)."""
    secs = session.scalars(
        select(models.Security).where(models.Security.symbol.in_(list(_DEMO_PRICES)))
    ).all()
    for sec in secs:
        close = _DEMO_PRICES.get(sec.symbol)
        if close is None:
            continue
        session.add(
            models.PriceBar(security_id=sec.id, bar_date=_PRICE_AS_OF, close=close, currency=sec.currency or "USD")
        )


def _seed_nav(session: Session) -> None:
    for snap_date, nav, cost_basis, spy_close in _DEMO_NAV:
        session.add(
            models.NavSnapshot(
                tenant_id=DEMO_TENANT_ID,
                portfolio_id=DEMO_PORTFOLIO_ID,
                snap_date=snap_date,
                nav=nav,
                cost_basis=cost_basis,
                external_flow=0.0,
                spy_close=spy_close,
            )
        )
