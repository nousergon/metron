"""CSV/manual account labels are scoped to their portfolio (metron-ops#383, FIN-02).

The default CSV import names its account "CSV" and manual entry names one too, and both
were unique per TENANT, so a second portfolio's import was written into the first
portfolio's account. Broker account numbers stay tenant-wide, so a re-sync elsewhere
still moves the account (metron-ops#192). All data is synthetic.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.db import models
from api.db.session import create_all
from api.services.account_identity import ambiguous_label_accounts
from api.services.persistence import persist_snapshot
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.broker_io.manual_entry import ManualPosition, build_manual_snapshot
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import CanonicalAccount

CSV_A = "date,type,symbol,quantity,price\n2026-01-05,BUY,AAA,10,100\n"
CSV_B = "date,type,symbol,quantity,price\n2026-01-06,BUY,BBB,5,50\n"


def _tenant_with_two_portfolios(session):
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    a = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="A")
    b = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="B")
    session.add_all([tenant, a, b])
    session.commit()
    return tenant.id, a.id, b.id


def _accounts(session, portfolio_id):
    return session.scalars(select(models.Account).where(models.Account.portfolio_id == portfolio_id)).all()


def _txns(session, account):
    return session.scalars(select(models.Transaction).where(models.Transaction.account_id == account.id)).all()


def test_default_csv_imports_into_two_portfolios_stay_separate(db_session):
    tenant_id, a, b = _tenant_with_two_portfolios(db_session)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=a, snapshot=parse_transactions_csv(CSV_A).snapshot)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=b, snapshot=parse_transactions_csv(CSV_B).snapshot)

    (acct_a,) = _accounts(db_session, a)
    (acct_b,) = _accounts(db_session, b)
    assert acct_a.id != acct_b.id
    assert acct_a.external_id == acct_b.external_id
    assert [float(t.quantity) for t in _txns(db_session, acct_a)] == [10.0]
    assert [float(t.quantity) for t in _txns(db_session, acct_b)] == [5.0]


def test_reimport_into_each_portfolio_is_idempotent(db_session):
    tenant_id, a, b = _tenant_with_two_portfolios(db_session)
    for _ in range(2):
        persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=a, snapshot=parse_transactions_csv(CSV_A).snapshot)
        result = persist_snapshot(
            db_session, tenant_id=tenant_id, portfolio_id=b, snapshot=parse_transactions_csv(CSV_B).snapshot
        )

    assert result.accounts_created == 0
    assert result.transactions_inserted == 0
    assert len(_accounts(db_session, a)) == 1 and len(_accounts(db_session, b)) == 1
    assert len(_txns(db_session, _accounts(db_session, b)[0])) == 1


def test_manual_entries_in_two_portfolios_stay_separate(db_session):
    tenant_id, a, b = _tenant_with_two_portfolios(db_session)
    for pid in (a, b):
        snapshot = build_manual_snapshot(
            ManualPosition(ticker="AAA", quantity=1, cost_basis=10, trade_date=date(2026, 1, 5), currency="USD")
        )
        persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=snapshot)

    assert len(_accounts(db_session, a)) == 1
    assert len(_accounts(db_session, b)) == 1


def test_broker_account_number_stays_tenant_wide_and_moves(db_session):
    tenant_id, a, b = _tenant_with_two_portfolios(db_session)
    snapshot = ConnectorSnapshot(source="ibkr_flex", accounts=[CanonicalAccount(number="U1")])
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=a, snapshot=snapshot)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=b, snapshot=snapshot)

    rows = db_session.scalars(select(models.Account).where(models.Account.external_id == "U1")).all()
    assert [(r.portfolio_id, r.identity_scope) for r in rows] == [(b, models.TENANT_SCOPE)]


def test_scope_is_set_from_broker_and_portfolio_at_insert(db_session):
    tenant_id, a, _b = _tenant_with_two_portfolios(db_session)
    csv = models.Account(tenant_id=tenant_id, portfolio_id=a, broker="csv", external_id="CSV")
    flex = models.Account(tenant_id=tenant_id, portfolio_id=a, broker="ibkr_flex", external_id="U2")
    db_session.add_all([csv, flex])
    db_session.commit()

    assert csv.identity_scope == a.hex
    assert flex.identity_scope == models.TENANT_SCOPE


@pytest.mark.parametrize(("broker", "scope_of"), [("csv", "portfolio"), ("ibkr_flex", "tenant")])
def test_database_enforces_uniqueness_within_the_scope(db_session, broker, scope_of):
    tenant_id, a, b = _tenant_with_two_portfolios(db_session)
    db_session.add(models.Account(tenant_id=tenant_id, portfolio_id=a, broker=broker, external_id="X"))
    db_session.commit()

    db_session.add(models.Account(tenant_id=tenant_id, portfolio_id=a, broker=broker, external_id="X"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(models.Account(tenant_id=tenant_id, portfolio_id=b, broker=broker, external_id="X"))
    if scope_of == "portfolio":
        db_session.commit()
    else:
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


def test_ambiguous_report_lists_label_accounts_in_multi_portfolio_tenants_only(db_session):
    tenant_id, a, _b = _tenant_with_two_portfolios(db_session)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=a, snapshot=parse_transactions_csv(CSV_A).snapshot)
    solo = models.Tenant(id=uuid.uuid4(), name="solo")
    solo_pf = models.Portfolio(id=uuid.uuid4(), tenant_id=solo.id, name="Only")
    db_session.add_all([solo, solo_pf])
    db_session.commit()
    persist_snapshot(db_session, tenant_id=solo.id, portfolio_id=solo_pf.id, snapshot=parse_transactions_csv(CSV_B).snapshot)

    (row,) = ambiguous_label_accounts(db_session)
    assert row.tenant_id == tenant_id and row.portfolio_id == a
    assert row.portfolios_in_tenant == 2
    assert row.transactions == 1
    assert row.first_trade == row.last_trade == date(2026, 1, 5)


def _legacy_sqlite():
    """A SQLite DB whose ``accounts`` table predates identity_scope, with its old
    tenant-wide UNIQUE table constraint and rows that reference it."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    create_all(engine)
    tenant, a, b = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE accounts")
        conn.exec_driver_sql(
            "CREATE TABLE accounts (id CHAR(32) NOT NULL PRIMARY KEY, tenant_id CHAR(32) NOT NULL, "
            "portfolio_id CHAR(32) NOT NULL, broker VARCHAR(50) NOT NULL, external_id VARCHAR(120) NOT NULL, "
            "name VARCHAR(200), currency VARCHAR(3) NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT uq_account_source UNIQUE (tenant_id, broker, external_id))"
        )
        conn.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 't')"), {"t": tenant})
        for pid in (a, b):
            conn.execute(
                text("INSERT INTO portfolios (id, tenant_id, name, base_currency) VALUES (:p, :t, 'P', 'USD')"),
                {"p": pid, "t": tenant},
            )
        conn.execute(
            text(
                "INSERT INTO accounts (id, tenant_id, portfolio_id, broker, external_id, currency) VALUES "
                "(:c, :t, :a, 'csv', 'CSV', 'USD'), (:f, :t, :a, 'ibkr_flex', 'U1', 'USD')"
            ),
            {"c": uuid.uuid4().hex, "f": uuid.uuid4().hex, "t": tenant, "a": a},
        )
    return engine, uuid.UUID(tenant), uuid.UUID(a), uuid.UUID(b)


def test_legacy_sqlite_accounts_table_is_rebuilt_without_moving_a_row():
    engine, tenant_id, a, b = _legacy_sqlite()
    create_all(engine)

    names = {c["name"] for c in inspect(engine).get_columns("accounts")}
    assert {"identity_scope", "holdings_synced_on"} <= names
    with Session(engine) as session:
        rows = {r.broker: r for r in session.scalars(select(models.Account)).all()}
        assert rows["csv"].identity_scope == a.hex and rows["csv"].portfolio_id == a
        assert rows["ibkr_flex"].identity_scope == models.TENANT_SCOPE

        persist_snapshot(session, tenant_id=tenant_id, portfolio_id=b, snapshot=parse_transactions_csv(CSV_B).snapshot)
        assert len(_accounts(session, b)) == 1, "the old tenant-wide constraint is gone"

    create_all(engine)  # a second start is a no-op
