"""Layer-1 custodian reconciliation (metron-ops#216).

Reuses the connectors' own recorded fixtures (Flex XML from
tests/test_ibkr_flex_connector, the fake SnapTrade reader from
tests/test_connectors_snaptrade) exactly like tests/test_broker_sync.py — only the
network boundary is mocked, so a real connector parses/normalizes the data. Pure, no
network.
"""

from __future__ import annotations

import pytest

from api.db import models
from api.services import broker_sync, reconciliation
from tests.test_broker_sync import _reader_source, _seed_portfolio
from tests.test_connectors_snaptrade import _FakeReader
from tests.test_ibkr_flex_connector import STATEMENT

FLEX_ACCT = "U33333333"


@pytest.fixture()
def flex_ok(monkeypatch):
    monkeypatch.setattr(
        "portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml",
        lambda *a, **k: STATEMENT,
    )
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")


@pytest.fixture()
def snaptrade_ok(monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    monkeypatch.setattr(broker_sync, "SnapTradeReader", _reader_source(_FakeReader))


def _security(session, symbol, currency="USD"):
    sec = models.Security(symbol=symbol, currency=currency)
    session.add(sec)
    session.flush()
    return sec


def _position(session, *, account, security, quantity, avg_cost, as_of):
    pos = models.Position(
        tenant_id=account.tenant_id,
        account_id=account.id,
        security_id=security.id,
        quantity=quantity,
        avg_cost=avg_cost,
        as_of=as_of,
    )
    session.add(pos)
    session.flush()
    return pos


def select_account(session, portfolio, broker, external_id):
    from sqlalchemy import select

    return session.scalars(
        select(models.Account).where(
            models.Account.tenant_id == portfolio.tenant_id,
            models.Account.broker == broker,
            models.Account.external_id == external_id,
        )
    ).first()


def test_no_breaks_when_db_matches_broker(db_session, flex_ok):
    """RKLB/SPY quantity + cost basis + account cash all match the Flex fixture exactly
    — a clean reconciliation records nothing and sends no alert."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600  # matches EquitySummaryByReportDateInBase reportDate 20260605
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=20, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.breaks_open == 0
    assert result.breaks_new == 0
    assert not result.fetch_failures
    assert db_session.query(models.ReconciliationBreak).count() == 0


def test_quantity_mismatch_creates_break_and_alerts(db_session, flex_ok, monkeypatch):
    alerts = []
    monkeypatch.setattr(reconciliation, "send_telegram_alert", lambda text: alerts.append(text) or True)

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    # Metron thinks 90 RKLB shares; the broker reports 100 — a real break.
    _position(db_session, account=account, security=rklb, quantity=90, avg_cost=22.222222, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.breaks_new == 1
    assert result.breaks_open == 1
    row = db_session.query(models.ReconciliationBreak).one()
    assert row.break_type == "quantity"
    assert float(row.metron_value) == 90
    assert float(row.broker_value) == 100
    assert row.alerted_at is not None
    assert len(alerts) == 1
    assert "quantity" in alerts[0]


def test_small_cost_basis_diff_within_tolerance_is_not_a_break(db_session, flex_ok):
    """RKLB broker cost basis is 2000; Metron's avg_cost*qty is 1999.99 — a cent of
    rounding, well inside the configured tolerance (max($1, 5bps))."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=19.9999, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.breaks_open == 0


def test_large_cost_basis_diff_creates_break(db_session, flex_ok):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=15, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    reconciliation.reconcile_portfolio(db_session, pf)

    breaks = db_session.query(models.ReconciliationBreak).all()
    assert any(b.break_type == "cost_basis" for b in breaks)


def test_missing_position_at_broker_flagged(db_session, flex_ok):
    """Metron holds a position (TSLA) the broker's fresh snapshot no longer reports at
    all — the account sold out and Metron never re-synced."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    tsla = _security(db_session, "TSLA")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=20, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    _position(db_session, account=account, security=tsla, quantity=5, avg_cost=200, as_of=pf.created_at.date())
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    breaks = db_session.query(models.ReconciliationBreak).all()
    assert any(b.break_type == "missing_at_broker" and b.security_id == tsla.id for b in breaks)
    assert result.breaks_new == 1


def test_missing_position_in_metron_flagged(db_session, flex_ok):
    """The broker reports RKLB but Metron has no position row for it at all."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    spy = _security(db_session, "SPY")
    _security(db_session, "RKLB")  # security master exists, but no Position row
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    reconciliation.reconcile_portfolio(db_session, pf)

    breaks = db_session.query(models.ReconciliationBreak).all()
    assert any(b.break_type == "missing_in_metron" for b in breaks)


def test_cash_mismatch_flagged(db_session, flex_ok):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 100  # broker fixture says 600 as of the latest report date
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=20, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    reconciliation.reconcile_portfolio(db_session, pf)

    breaks = db_session.query(models.ReconciliationBreak).all()
    cash_breaks = [b for b in breaks if b.break_type == "cash"]
    assert len(cash_breaks) == 1
    assert cash_breaks[0].security_id is None
    assert float(cash_breaks[0].metron_value) == 100
    assert float(cash_breaks[0].broker_value) == 600


def test_break_resolves_when_it_stops_reproducing(db_session, flex_ok):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    pos = _position(db_session, account=account, security=rklb, quantity=90, avg_cost=22.222222, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    result1 = reconciliation.reconcile_portfolio(db_session, pf)
    assert result1.breaks_new == 1

    # Fix the drift (as if the regular nightly sync had corrected it) and re-run.
    pos.quantity = 100
    pos.avg_cost = 20
    db_session.commit()
    result2 = reconciliation.reconcile_portfolio(db_session, pf)

    assert result2.breaks_open == 0
    assert result2.breaks_resolved == 1
    row = db_session.query(models.ReconciliationBreak).one()
    assert row.resolved_at is not None


def test_rerun_same_day_does_not_duplicate_or_realert(db_session, flex_ok, monkeypatch):
    alerts = []
    monkeypatch.setattr(reconciliation, "send_telegram_alert", lambda text: alerts.append(text) or True)

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=90, avg_cost=22.222222, as_of=pf.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    db_session.commit()

    reconciliation.reconcile_portfolio(db_session, pf)
    reconciliation.reconcile_portfolio(db_session, pf)

    assert db_session.query(models.ReconciliationBreak).count() == 1
    assert len(alerts) == 1  # second run: already-alerted, not new/reopened


def test_fetch_failure_alerts_and_reports_without_raising(db_session, monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")

    def _boom(*a, **k):
        raise RuntimeError("IBKR unreachable")

    monkeypatch.setattr("portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml", _boom)
    alerts = []
    monkeypatch.setattr(reconciliation, "send_telegram_alert", lambda text: alerts.append(text) or True)

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.fetch_failures
    assert any("IBKR unreachable" in msg for msg in result.fetch_failures)
    assert len(alerts) == 1


def test_snaptrade_quantity_mismatch_detected(db_session, snaptrade_ok):
    pf = _seed_portfolio(db_session, broker="snaptrade")
    account = select_account(db_session, pf, "snaptrade", "U001")
    aapl = _security(db_session, "AAPL")
    rklb = _security(db_session, "RKLB")
    _position(db_session, account=account, security=aapl, quantity=5, avg_cost=100, as_of=pf.created_at.date())
    _position(db_session, account=account, security=rklb, quantity=50, avg_cost=20, as_of=pf.created_at.date())
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    breaks = db_session.query(models.ReconciliationBreak).all()
    assert any(b.break_type == "quantity" for b in breaks)
    assert result.accounts_covered >= 1


def test_reconcile_all_covers_every_non_reference_portfolio(db_session, flex_ok):
    pf1 = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf1, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(db_session, "RKLB")
    spy = _security(db_session, "SPY")
    _position(db_session, account=account, security=rklb, quantity=100, avg_cost=20, as_of=pf1.created_at.date())
    _position(db_session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf1.created_at.date())
    db_session.commit()

    total = reconciliation.reconcile_all(db_session)

    assert total.portfolios_checked >= 1
