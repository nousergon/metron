"""Snapshot replacement scope is per stated account, not per row (metron-ops#382, FIN-01).

A successful FULL_REFRESH that states an account empty used to leave its old positions
and open lots alive, because the delete scope was derived from the rows the snapshot
carried. These tests drive the real persistence bridge on the SQLite test DB through
every case the issue names: populated to empty holdings, populated to empty lots,
missing or unsupported lot detail, a partial fetch, a failed fetch, and an untouched
account. All data is synthetic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import select

from api.db import models
from api.services import broker_sync, persistence
from api.services.persistence import persist_snapshot
from portfolio_analytics.ingestion.base import SYNC_INCREMENTAL, ConnectorSnapshot
from portfolio_analytics.ingestion.ibkr_flex_connector import IbkrFlexConnector, replacement_scope
from portfolio_analytics.ingestion.reference_connector import ACCOUNT_NUMBER, artifact_to_snapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalHolding,
    CanonicalOpenLot,
    CanonicalSecurity,
)
from portfolio_analytics.ingestion.snaptrade import SnapTradeConnector

SEC = CanonicalSecurity(security_id="EQ:AAA:USD", ticker="AAA", currency="USD")
REPORT_DAY = datetime(2026, 10, 30)


def _portfolio(session):
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="P")
    session.add_all([tenant, portfolio])
    session.commit()
    return tenant.id, portfolio.id


def _holding(number, qty=10.0):
    return CanonicalHolding(
        account_number=number, security_id=SEC.security_id, quantity=qty, avg_cost=1.0, cost_basis=qty, currency="USD"
    )


def _lot(number, qty=10.0):
    return CanonicalOpenLot(
        account_number=number,
        security_id=SEC.security_id,
        ticker="AAA",
        quantity=qty,
        open_date=date(2026, 1, 2),
        cost_basis=qty,
        currency="USD",
    )


def _account(number):
    return CanonicalAccount(number=number, as_of=REPORT_DAY, cash_usd=10000.0)


def _populated(numbers):
    return ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[_account(n) for n in numbers],
        securities=[SEC],
        holdings=[_holding(n) for n in numbers],
        open_lots=[_lot(n) for n in numbers],
        holdings_complete_for=frozenset(numbers),
        open_lots_complete_for=frozenset(numbers),
    )


def _rows(session, model, number):
    acct = session.scalars(select(models.Account).where(models.Account.external_id == number)).one()
    return session.scalars(select(model).where(model.account_id == acct.id)).all()


def _account_row(session, number):
    return session.scalars(select(models.Account).where(models.Account.external_id == number)).one()


@pytest.fixture()
def seeded(db_session):
    tenant_id, portfolio_id = _portfolio(db_session)
    persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=_populated(["U1", "U2"]))
    assert len(_rows(db_session, models.Position, "U1")) == 1
    assert len(_rows(db_session, models.OpenLot, "U1")) == 1

    def persist(snapshot):
        return persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=portfolio_id, snapshot=snapshot)

    return db_session, persist


def test_populated_to_empty_holdings_clears_positions_and_lots(seeded):
    session, persist = seeded
    result = persist(
        ConnectorSnapshot(source="ibkr_flex", accounts=[_account("U1")], holdings_complete_for=frozenset({"U1"}))
    )

    assert _rows(session, models.Position, "U1") == []
    assert _rows(session, models.OpenLot, "U1") == [], "no open position means no open lot"
    assert result.positions_imported == 0
    assert result.positions_accounts_replaced == 1
    assert _account_row(session, "U1").holdings_synced_on == REPORT_DAY.date()
    assert _account_row(session, "U1").open_lots_synced_on == REPORT_DAY.date()
    assert float(_account_row(session, "U1").cash_balance_usd) == 10000.0


def test_untouched_account_keeps_its_rows(seeded):
    session, persist = seeded
    persist(ConnectorSnapshot(source="ibkr_flex", accounts=[_account("U1")], holdings_complete_for=frozenset({"U1"})))

    assert len(_rows(session, models.Position, "U2")) == 1
    assert len(_rows(session, models.OpenLot, "U2")) == 1


def test_populated_to_empty_authoritative_lots_clears_lots_only(seeded):
    session, persist = seeded
    persist(
        ConnectorSnapshot(
            source="ibkr_flex",
            accounts=[_account("U1")],
            securities=[SEC],
            holdings=[_holding("U1")],
            holdings_complete_for=frozenset({"U1"}),
            open_lots_complete_for=frozenset({"U1"}),
        )
    )

    assert len(_rows(session, models.Position, "U1")) == 1
    assert _rows(session, models.OpenLot, "U1") == []


def test_unsupported_lot_detail_preserves_lots_of_a_held_account(seeded):
    session, persist = seeded
    result = persist(
        ConnectorSnapshot(
            source="ibkr_flex",
            accounts=[_account("U1")],
            securities=[SEC],
            holdings=[_holding("U1", qty=5.0)],
            holdings_complete_for=frozenset({"U1"}),
            open_lots_complete_for=None,
        )
    )

    assert [float(p.quantity) for p in _rows(session, models.Position, "U1")] == [5.0]
    assert len(_rows(session, models.OpenLot, "U1")) == 1, "missing lot detail is not an empty lot set"
    assert result.open_lots_accounts_replaced == 0


def test_partial_fetch_preserves_the_unstated_account_and_reports_it(seeded):
    session, persist = seeded
    result = persist(
        ConnectorSnapshot(
            source="ibkr_flex",
            accounts=[_account("U1"), _account("U2")],
            holdings_complete_for=frozenset({"U1"}),
        )
    )

    assert _rows(session, models.Position, "U1") == []
    assert len(_rows(session, models.Position, "U2")) == 1
    assert result.holdings_not_stated == ["U2"]


def test_undeclared_snapshot_never_reads_an_absence_as_empty(seeded):
    session, persist = seeded
    persist(ConnectorSnapshot(source="ibkr_flex", accounts=[_account("U1")]))

    assert len(_rows(session, models.Position, "U1")) == 1
    assert len(_rows(session, models.OpenLot, "U1")) == 1


def test_failed_fetch_replaces_nothing_and_is_marked_degraded(seeded):
    session, persist = seeded
    result = persist(ConnectorSnapshot(source="ibkr_flex", error="503", holdings_complete_for=frozenset({"U1"})))

    assert result.degraded == "503"
    assert len(_rows(session, models.Position, "U1")) == 1
    assert len(_rows(session, models.OpenLot, "U1")) == 1


def test_incremental_snapshot_declares_nothing(seeded):
    session, persist = seeded
    persist(
        ConnectorSnapshot(
            source="ibkr_flex",
            accounts=[_account("U1")],
            sync_mode=SYNC_INCREMENTAL,
            holdings_complete_for=frozenset({"U1"}),
        )
    )

    assert len(_rows(session, models.Position, "U1")) == 1


def test_replacement_is_atomic(seeded, monkeypatch):
    session, persist = seeded

    def boom(*args, **kwargs):
        raise RuntimeError("lot write failed")

    monkeypatch.setattr(persistence, "_replace_open_lots", boom)
    with pytest.raises(RuntimeError):
        persist(ConnectorSnapshot(source="ibkr_flex", accounts=[_account("U1")], holdings_complete_for=frozenset({"U1"})))
    session.rollback()

    assert len(_rows(session, models.Position, "U1")) == 1, "a failed persist must not leave a half-cleared account"


# ── connector declarations ──────────────────────────────────────────────────


def _flex(statements):
    return f"<FlexQueryResponse><FlexStatements>{statements}</FlexStatements></FlexQueryResponse>"


def test_flex_declares_holdings_for_statements_with_an_open_positions_section():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(
        _flex(
            '<FlexStatement accountId="U1"><OpenPositions /></FlexStatement>'
            '<FlexStatement accountId="U2"><CashTransactions /></FlexStatement>'
        )
    )
    assert replacement_scope(root) == (frozenset({"U1"}), None)


def test_flex_declares_lots_only_when_lot_detail_is_present():
    xml = _flex(
        '<FlexStatement accountId="U1"><OpenPositions>'
        '<OpenPosition accountId="U1" symbol="AAA" position="10" levelOfDetail="SUMMARY" />'
        '<OpenPosition accountId="U1" symbol="AAA" position="10" levelOfDetail="LOT" openDateTime="20260102" />'
        "</OpenPositions></FlexStatement>"
        '<FlexStatement accountId="U2"><OpenPositions /></FlexStatement>'
    )
    snapshot = IbkrFlexConnector("t", "q", fetcher=lambda: xml, persist_bronze=False).sync()

    assert snapshot.holdings_complete_for == frozenset({"U1", "U2"})
    assert snapshot.open_lots_complete_for == frozenset({"U1", "U2"})


def test_snaptrade_declares_only_accounts_whose_holdings_fetch_succeeded():
    class Reader:
        last_holdings_fetched_accounts: set[str] = set()

        def get_accounts(self):
            return [{"id": "a", "number": "N1"}, {"id": "b", "number": "N2"}]

        def get_all_holdings(self):
            self.last_holdings_fetched_accounts = {"N1"}
            return pd.DataFrame()

        def get_all_activities(self):
            return []

    assert SnapTradeConnector(Reader()).sync().holdings_complete_for == frozenset({"N1"})


def test_snaptrade_reader_without_fetch_record_declares_nothing():
    class Reader:
        def get_accounts(self):
            return [{"id": "a", "number": "N1"}]

        def get_all_holdings(self):
            return pd.DataFrame()

        def get_all_activities(self):
            return []

    assert SnapTradeConnector(Reader()).sync().holdings_complete_for is None


def test_snaptrade_reader_records_per_account_fetch_success(monkeypatch):
    from portfolio_analytics.broker_io.snaptrade_reader import SnapTradeReader

    reader = SnapTradeReader.__new__(SnapTradeReader)
    accounts = [{"id": "a", "number": "N1", "name": "A", "type": "T"}, {"id": "b", "number": "N2", "name": "B", "type": "T"}]
    monkeypatch.setattr(reader, "get_accounts", lambda: accounts, raising=False)

    def holdings(account_id):
        if account_id == "b":
            raise RuntimeError("timeout")
        return []

    monkeypatch.setattr(reader, "get_holdings", holdings, raising=False)
    reader.get_all_holdings()

    assert reader.last_holdings_fetched_accounts == {"N1"}


def test_reference_artifact_declares_its_one_account_only_when_it_lists_positions():
    assert artifact_to_snapshot({"positions": []}).holdings_complete_for == frozenset({ACCOUNT_NUMBER})
    assert artifact_to_snapshot({}).holdings_complete_for is None


# ── freshness of zero-position accounts ─────────────────────────────────────


def test_zero_position_account_is_measured_for_sync_freshness(db_session):
    tenant_id, portfolio_id = _portfolio(db_session)
    persist_snapshot(
        db_session,
        tenant_id=tenant_id,
        portfolio_id=portfolio_id,
        snapshot=ConnectorSnapshot(
            source="ibkr_flex", accounts=[_account("U9")], holdings_complete_for=frozenset({"U9"})
        ),
    )
    assert _rows(db_session, models.Position, "U9") == []

    assert broker_sync.stale_broker_accounts(db_session, today=date(2026, 10, 30)) == []
    stale = broker_sync.stale_broker_accounts(db_session, today=date(2026, 11, 5))
    assert [s.as_of for s in stale] == [REPORT_DAY.date()]
