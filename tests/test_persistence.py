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


def test_stored_lot_account_does_not_suppress_replay_of_other_accounts(db_session):
    """A ticker held in BOTH an IBKR stored-lot account AND a replayable (CSV/SnapTrade)
    account must surface realized gains from BOTH — the stored-lot account is excluded from
    replay PER ACCOUNT, never portfolio-wide (metron-ops#75).

    Brian's RKLB case: ~$31k of 2025 LT gains came from an E-Trade (replayable) account and
    a separate IBKR stored lot carried ~$12.5k more. If the stored-lot exclusion were
    portfolio-wide instead of per-account, the entire E-Trade realized history would vanish
    and LT realized would read far too low. This guards the per-account boundary."""
    from datetime import date

    from api.services import analytics
    from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
    from portfolio_analytics.domain.ledger import RealizedGain
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_id = _make_portfolio(db_session)

    # 1) Replayable account (CSV — no stored lots): RKLB bought 2024, sold long-term 2025.
    csv = (
        "date,type,symbol,quantity,price,amount,fees\n"
        "2024-06-25,BUY,RKLB,100,5,500,0\n"
        "2025-07-01,SELL,RKLB,100,40,4000,0\n"
    )
    persist_snapshot(
        db_session, tenant_id=tenant_id, portfolio_id=portfolio_id,
        snapshot=parse_transactions_csv(csv).snapshot,
    )
    # 2) IBKR account with an authoritative stored RKLB lot (no replayable feed).
    persist_snapshot(
        db_session, tenant_id=tenant_id, portfolio_id=portfolio_id,
        snapshot=ConnectorSnapshot(
            source="ibkr_flex",
            accounts=[CanonicalAccount(number="U24215043", label="IBKR", tax_treatment="taxable")],
            realized_lots=[(
                "U24215043",
                RealizedGain(
                    ticker="RKLB", open_date=date(2024, 6, 25), close_date=date(2026, 5, 20),
                    quantity=102, proceeds=13004.0, cost_basis=600.0,
                ),
            )],
        ),
    )

    lots = analytics.realized(db_session, tenant_id, portfolio_id)
    rklb = [r for r in lots if r.ticker == "RKLB"]
    # BOTH sources surface: the replayed 2025 LT sale AND the IBKR stored 2026 LT lot.
    assert len(rklb) == 2, "the stored-lot account must not suppress the replayed account"
    replayed = next(r for r in rklb if r.close_date == date(2025, 7, 1))
    stored = next(r for r in rklb if r.close_date == date(2026, 5, 20))
    assert replayed.gain == 4000.0 - 500.0 and replayed.long_term is True
    assert stored.gain == 13004.0 - 600.0 and stored.long_term is True

    # income(): each year's LT bucket carries its own source — neither is lost.
    years = {y.year: y for y in analytics.income(db_session, tenant_id, portfolio_id)}
    assert years[2025].realized_lt == 4000.0 - 500.0
    assert years[2026].realized_lt == 13004.0 - 600.0


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


def _ibkr_snapshot_with_lots(*, msft_lot: bool):
    """An IBKR snapshot holding AAPL + MSFT; AAPL always has a covering lot, MSFT only
    when ``msft_lot`` (so the uncovered-holding path can be exercised)."""
    from datetime import date

    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import (
        CanonicalAccount,
        CanonicalHolding,
        CanonicalOpenLot,
        CanonicalSecurity,
    )

    lots = [CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL", quantity=10, open_date=date(2025, 1, 1), cost_basis=1000)]
    if msft_lot:
        lots.append(CanonicalOpenLot(account_number="U1", security_id="EQ:MSFT:USD", ticker="MSFT", quantity=5, open_date=date(2025, 2, 1), cost_basis=1800))
    return ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", label="IBKR")],
        securities=[CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL"), CanonicalSecurity(security_id="EQ:MSFT:USD", ticker="MSFT")],
        holdings=[
            CanonicalHolding(account_number="U1", security_id="EQ:AAPL:USD", quantity=10, market_value_local=1500),
            CanonicalHolding(account_number="U1", security_id="EQ:MSFT:USD", quantity=5, market_value_local=2000),
        ],
        open_lots=lots,
    )


def test_nav_history_estimated_flags_uncovered_holdings(db_session):
    """A holding with no covering lot → the NAV history is an estimate (metron-ops#74)."""
    from api.services import performance

    tenant_id, portfolio_id = _make_portfolio(db_session)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=_ibkr_snapshot_with_lots(msft_lot=False))
    estimated, note = performance.nav_history_estimated(db_session, tenant_id, portfolio_id)
    assert estimated is True and note is not None and "MSFT" in note and "AAPL" not in note


def test_nav_history_exact_when_every_holding_lot_covered(db_session):
    from api.services import performance

    tenant_id, portfolio_id = _make_portfolio(db_session)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=_ibkr_snapshot_with_lots(msft_lot=True))
    estimated, note = performance.nav_history_estimated(db_session, tenant_id, portfolio_id)
    assert estimated is False and note is None


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


def test_resync_reparents_account_to_new_portfolio(db_session):
    """metron-ops#192: _upsert_accounts matches existing rows by (tenant_id, broker,
    external_id) only. If that account was first created under portfolio A, a later
    sync targeting portfolio B for the same tenant must reparent the row to B, not
    silently leave it orphaned under A."""
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_a = _make_portfolio(db_session)
    portfolio_b = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant_id, name="Roth IRA")
    db_session.add(portfolio_b)
    db_session.commit()

    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U23568545", institution="Interactive Brokers", account_type="Roth IRA")],
    )
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_a, snapshot=snapshot)
    acct = db_session.scalars(select(models.Account).where(models.Account.external_id == "U23568545")).one()
    assert acct.portfolio_id == portfolio_a

    # Re-sync the same (broker, external_id) targeting portfolio B.
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_b.id, snapshot=snapshot)

    accounts = db_session.scalars(select(models.Account).where(models.Account.external_id == "U23568545")).all()
    assert len(accounts) == 1  # reparented in place, not duplicated
    assert accounts[0].portfolio_id == portfolio_b.id

    a_accounts = db_session.scalars(select(models.Account).where(models.Account.portfolio_id == portfolio_a)).all()
    b_accounts = db_session.scalars(select(models.Account).where(models.Account.portfolio_id == portfolio_b.id)).all()
    assert a_accounts == []
    assert [a.external_id for a in b_accounts] == ["U23568545"]


def test_resync_does_not_reparent_csv_account_with_colliding_label(db_session):
    """Companion to test_resync_reparents_account_to_new_portfolio: reparenting is
    only sound for sources with a brokerage-assigned, tenant-wide-stable external id
    (IBKR Flex, SnapTrade, OFX). CSV's external id is a free-text "account" column
    label the user types per import — two unrelated portfolios can legitimately both
    use the label "Roth" for two different real accounts, so a same-label match must
    NOT move the account out from under the portfolio it was created in."""
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_a = _make_portfolio(db_session)
    portfolio_b = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant_id, name="Other")
    db_session.add(portfolio_b)
    db_session.commit()

    snapshot = ConnectorSnapshot(source="csv", accounts=[CanonicalAccount(number="Roth")])
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_a, snapshot=snapshot)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_b.id, snapshot=snapshot)

    accounts = db_session.scalars(
        select(models.Account).where(models.Account.tenant_id == tenant_id, models.Account.external_id == "Roth")
    ).all()
    assert len(accounts) == 1  # DB uniqueness (tenant_id, broker, external_id) forbids a second row
    assert accounts[0].portfolio_id == portfolio_a  # left where it was, not moved to B


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


def test_persists_connector_cash_balance(db_session):
    """A snapshot-sourced connector's ``cash_usd`` (the NAV-minus-positions reconciling
    plug every one of Flex/SnapTrade/reference computes — see reference_connector.py,
    ibkr_flex_connector.py, snaptrade.py) was computed and then silently discarded
    before reaching the DB (no column existed to hold it). This undercounted every such
    account's displayed total by its cash balance (live case: $20.3k missing from the
    Crucible reference-rate sleeve). ``_upsert_accounts`` now persists it onto
    ``Account.cash_balance_usd`` at creation."""
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", institution="Interactive Brokers", nav_usd=50_000.0, cash_usd=20_300.0)],
    )
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)

    acct = db_session.scalars(select(models.Account).where(models.Account.external_id == "U1")).one()
    assert float(acct.cash_balance_usd) == 20_300.0


def test_resync_overwrites_cash_balance_unlike_institution(db_session):
    """Cash is a LIVE balance (unlike institution/account_type/tax_treatment, which are
    sticky tags a Settings edit must survive) — a re-sync must always take the
    connector's latest figure, even when the stored value is already non-null/non-zero,
    the opposite of the fill-blank-only convention used for the tagging fields."""
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    tenant_id, portfolio_id = _make_portfolio(db_session)
    first = ConnectorSnapshot(source="snaptrade", accounts=[CanonicalAccount(number="U2", cash_usd=1_000.0)])
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=first)
    acct = db_session.scalars(select(models.Account).where(models.Account.external_id == "U2")).one()
    assert float(acct.cash_balance_usd) == 1_000.0

    second = ConnectorSnapshot(source="snaptrade", accounts=[CanonicalAccount(number="U2", cash_usd=250.0)])
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=second)
    db_session.refresh(acct)
    assert float(acct.cash_balance_usd) == 250.0  # overwritten, not left at the higher stale value


def test_csv_account_cash_balance_stays_zero(db_session):
    """CSV/manual accounts never report a connector cash balance (``CanonicalAccount``
    defaults ``cash_usd`` to 0.0 for those sources) — persistence still writes that
    default, but the analytics layer must derive their actual cash from the ledger
    instead (see test_analytics_ledger.py), never trust this column for them."""
    tenant_id, portfolio_id = _make_portfolio(db_session)
    snapshot = parse_transactions_csv(CSV).snapshot
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)
    acct = db_session.scalars(select(models.Account).where(models.Account.broker == "csv")).one()
    assert float(acct.cash_balance_usd) == 0.0
