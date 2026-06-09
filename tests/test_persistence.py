"""Tests for the canonical-snapshot → multi-tenant-DB persistence bridge."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from api.db import models
from api.services.persistence import persist_snapshot
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv

CSV = """date,type,symbol,quantity,price,amount,fees
2024-01-15,BUY,AAPL,10,150,1500,1
2024-03-01,DIVIDEND,AAPL,,,4.40,
2024-06-01,SELL,AAPL,5,180,900,1
"""


def _make_portfolio(session, name="Taxable"):
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name=name)
    session.add_all([tenant, portfolio])
    session.commit()
    return tenant.id, portfolio.id


def test_persist_inserts_rows(db_session):
    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = parse_transactions_csv(CSV).snapshot
    result = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)

    assert result.accounts_created == 1
    assert result.securities_created == 1
    assert result.transactions_inserted == 3
    assert result.transactions_skipped == 0
    assert db_session.scalar(select(func.count()).select_from(models.Transaction)) == 3


def test_reimport_is_idempotent(db_session):
    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = parse_transactions_csv(CSV).snapshot
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)

    # Re-persist the identical snapshot: every row is a known source_key → all skipped.
    again = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    assert again.transactions_inserted == 0
    assert again.transactions_skipped == 3
    assert again.accounts_created == 0  # account upserted, not duplicated
    assert db_session.scalar(select(func.count()).select_from(models.Transaction)) == 3


def test_security_master_is_global(db_session):
    tenant_a, pf_a = _make_portfolio(db_session, name="A")
    tenant_b, pf_b = _make_portfolio(db_session, name="B")
    snapshot = parse_transactions_csv(CSV).snapshot

    first = persist_snapshot(db_session, tenant_id=tenant_a, portfolio_id=pf_a, snapshot=snapshot)
    second = persist_snapshot(db_session, tenant_id=tenant_b, portfolio_id=pf_b, snapshot=snapshot)

    assert first.securities_created == 1
    assert second.securities_created == 0  # AAPL master shared across tenants
    assert db_session.scalar(select(func.count()).select_from(models.Security)) == 1
    # Transactions are NOT shared — each tenant gets its own ledger.
    assert db_session.scalar(select(func.count()).select_from(models.Transaction)) == 6


def test_cash_transaction_has_null_security(db_session):
    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = parse_transactions_csv("date,type,amount\n2024-01-01,DEPOSIT,1000\n").snapshot
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    txn = db_session.scalars(select(models.Transaction)).one()
    assert txn.txn_type == "DEPOSIT"
    assert txn.security_id is None
