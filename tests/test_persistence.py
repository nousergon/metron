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


def test_persists_and_surfaces_broker_realized_lots(db_session):
    """IBKR-style closed lots (authoritative fifoPnlRealized, no replayable trades) are
    persisted idempotently and surface in realized() + income() — metron-ops#81."""
    from datetime import date

    from api.services import analytics
    from portfolio_analytics.domain.ledger import RealizedGain
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_id = _make_portfolio(db_session)
    rg = RealizedGain(
        ticker="RKLB", open_date=date(2024, 6, 25), close_date=date(2026, 5, 20),
        quantity=102, proceeds=13004.0, cost_basis=600.0,
    )
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U24215043", label="Lending Growth", tax_treatment="taxable")],
        realized_lots=[("U24215043", rg)],
    )
    result = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    assert result.realized_lots_inserted == 1
    # Idempotent on lot_key.
    again = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    assert again.realized_lots_inserted == 0
    assert db_session.scalar(select(func.count()).select_from(models.RealizedLot)) == 1

    # realized(): the lot appears, long-term (held ~23 months), USD → gain_base == gain.
    lots = analytics.realized(db_session, tenant_id, portfolio_id)
    assert len(lots) == 1
    lot = lots[0]
    assert lot.ticker == "RKLB" and lot.long_term is True
    assert lot.gain == 13004.0 - 600.0
    assert lot.gain_base == lot.gain

    # income(): the gain lands in the 2026 long-term bucket.
    y2026 = next(y for y in analytics.income(db_session, tenant_id, portfolio_id) if y.year == 2026)
    assert y2026.realized_lt == 13004.0 - 600.0
    assert y2026.realized_st == 0


def test_realized_lots_distinct_cost_basis_dont_collide(db_session):
    """IBKR emits DISTINCT closed lots sharing ticker/open/close/qty/proceeds but differing
    cost basis (two tax lots disposed together) — both must persist (the bare lot_key would
    collide → UNIQUE violation, the prod 500). An exact re-emission still collapses."""
    from datetime import date

    from portfolio_analytics.domain.ledger import RealizedGain
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_id = _make_portfolio(db_session)
    common = dict(ticker="ASML", open_date=date(2025, 12, 30), close_date=date(2026, 4, 24), quantity=1.0, proceeds=1236.7813)
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U23364707", label="Dividend Anchor")],
        realized_lots=[
            ("U23364707", RealizedGain(cost_basis=920.0, **common)),
            ("U23364707", RealizedGain(cost_basis=917.0, **common)),  # distinct lot, same bare key
            ("U23364707", RealizedGain(cost_basis=920.0, **common)),  # exact re-emission → collapses
        ],
    )
    result = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    assert result.realized_lots_inserted == 2  # 920 + 917; the duplicate 920 dropped
    assert db_session.scalar(select(func.count()).select_from(models.RealizedLot)) == 2


def test_open_lots_persisted_and_replaced_per_account(db_session):
    """Lot-level open positions persist and are REPLACED per account each sync (snapshot
    semantics) — metron-ops#74."""
    from datetime import date

    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount, CanonicalOpenLot

    tenant_id, portfolio_id = _make_portfolio(db_session)
    acct = CanonicalAccount(number="U1", label="IBKR")
    snap1 = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[acct],
        open_lots=[
            CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL", quantity=6, open_date=date(2025, 1, 15), cost_basis=900),
            CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL", quantity=4, open_date=date(2025, 12, 19), cost_basis=600),
        ],
    )
    r1 = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snap1)
    assert r1.open_lots_imported == 2
    assert db_session.scalar(select(func.count()).select_from(models.OpenLot)) == 2

    # Re-sync the same account with ONE lot → the account's lots are replaced, not unioned.
    snap2 = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[acct],
        open_lots=[CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL", quantity=10, open_date=date(2026, 1, 2), cost_basis=1500)],
    )
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snap2)
    rows = db_session.scalars(select(models.OpenLot)).all()
    assert len(rows) == 1 and float(rows[0].quantity) == 10 and rows[0].open_date == date(2026, 1, 2)


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


def test_persists_foreign_symbology_and_account_metadata(db_session):
    """A snapshot-sourced (Flex) holding persists the listing exchange + resolved
    yfinance symbol, the account's institution/type/tax_treatment, and the broker's
    native market value — all of which were discarded before the multicurrency work."""
    from datetime import datetime

    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount, CanonicalHolding, CanonicalSecurity

    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", institution="Interactive Brokers", account_type="Roth IRA", tax_treatment="tax_exempt", currency="USD")],
        securities=[CanonicalSecurity(security_id="EQ:1299:HKD", ticker="1299", currency="HKD", exchange="SEHK")],
        holdings=[CanonicalHolding(account_number="U1", security_id="EQ:1299:HKD", quantity=100, avg_cost=60, cost_basis=6000, market_value_local=7000.0, currency="HKD", as_of=datetime(2026, 6, 1))],
    )
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)

    sec = db_session.scalars(select(models.Security).where(models.Security.symbol == "1299")).one()
    assert sec.exchange == "SEHK" and sec.yf_symbol == "1299.HK"
    acct = db_session.scalars(select(models.Account).where(models.Account.external_id == "U1")).one()
    assert acct.institution == "Interactive Brokers" and acct.account_type == "Roth IRA" and acct.tax_treatment == "tax_exempt"
    pos = db_session.scalars(select(models.Position)).one()
    assert float(pos.market_value_local) == 7000.0
    assert float(pos.market_price) == 70.0  # 7000 / 100
