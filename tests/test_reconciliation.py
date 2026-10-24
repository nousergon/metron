"""Layer-1 custodian reconciliation (metron-ops#216).

Reuses the connectors' own recorded fixtures (Flex XML from
tests/test_ibkr_flex_connector, the fake SnapTrade reader from
tests/test_connectors_snaptrade) exactly like tests/test_broker_sync.py — only the
network boundary is mocked, so a real connector parses/normalizes the data. Pure, no
network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)

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
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)

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
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)

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


# ── fetch-status + staleness escalation (metron-ops#274) ─────────────────────


def _fail_flex(monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")

    def _boom(*a, **k):
        raise RuntimeError("IBKR unreachable")

    monkeypatch.setattr("portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml", _boom)


def test_successful_fetch_records_status(db_session, flex_ok):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    reconciliation.reconcile_portfolio(db_session, pf)

    row = db_session.query(models.ReconciliationFetchStatus).one()
    assert row.broker == "ibkr_flex"
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0


def test_first_fetch_failure_alerts_but_does_not_escalate(db_session, monkeypatch):
    """One bad night is a busy upstream, not a dead detector — alert, don't go red."""
    _fail_flex(monkeypatch)
    alerts: list[str] = []
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.fetch_failures  # recorded and alerted
    assert len(alerts) == 1  # visibility unchanged
    assert not result.stale_fetches  # but NOT escalated — this is the whole point
    row = db_session.query(models.ReconciliationFetchStatus).one()
    assert row.consecutive_failures == 1
    assert "IBKR unreachable" in row.last_error


def test_fetch_failure_escalates_once_the_staleness_window_is_exceeded(db_session, monkeypatch):
    """Past `reconciliation_stale_hours` with no good fetch, the unit must go red."""
    _fail_flex(monkeypatch)
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: True)
    pf = _seed_portfolio(db_session, broker="ibkr_flex")

    reconciliation.reconcile_portfolio(db_session, pf)  # first failure — creates the row
    row = db_session.query(models.ReconciliationFetchStatus).one()
    row.first_seen_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=40)
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)
    assert result.stale_fetches
    assert db_session.query(models.ReconciliationFetchStatus).one().consecutive_failures == 2


def test_success_resets_the_staleness_clock(db_session, monkeypatch, flex_ok):
    """A recovered broker stops being stale immediately, however old the last failure."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    db_session.add(
        models.ReconciliationFetchStatus(
            tenant_id=pf.tenant_id,
            portfolio_id=pf.id,
            broker="ibkr_flex",
            consecutive_failures=5,
            first_seen_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=9),
        )
    )
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert not result.fetch_failures
    assert not result.stale_fetches
    row = db_session.query(models.ReconciliationFetchStatus).one()
    assert row.consecutive_failures == 0
    assert row.last_success_at is not None


def test_never_successful_broker_still_escalates(db_session, monkeypatch):
    """A broker broken since the day it was wired has a NULL last_success_at; falling
    back to first_seen_at is what stops that reading as 'not stale' forever."""
    _fail_flex(monkeypatch)
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: True)
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    db_session.add(
        models.ReconciliationFetchStatus(
            tenant_id=pf.tenant_id,
            portfolio_id=pf.id,
            broker="ibkr_flex",
            first_seen_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=4),
        )
    )
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.stale_fetches
    row = db_session.query(models.ReconciliationFetchStatus).one()
    assert row.last_success_at is None


# ── Custodian NAV observation (MET-01, observe mode) ─────────────────────────────
# The Flex fixture's latest EquitySummaryByReportDateInBase is reportDate 20260605,
# total 10000, cash 600. Metron's side is the account NAV snapshot for that date
# (holdings value) plus the account's persisted cash.

NAV_DATE = datetime(2026, 6, 5).date()


def _account_snapshot(session, account, *, snap_date, nav):
    session.add(
        models.AccountNavSnapshot(
            tenant_id=account.tenant_id, portfolio_id=account.portfolio_id, account_id=account.id,
            snap_date=snap_date, nav=nav,
        )
    )
    session.flush()


def _seed_matching_layer1(session):
    """A portfolio whose layer-1 checks are clean, so only the NAV observation varies."""
    pf = _seed_portfolio(session, broker="ibkr_flex")
    account = select_account(session, pf, "ibkr_flex", FLEX_ACCT)
    account.cash_balance_usd = 600
    rklb = _security(session, "RKLB")
    spy = _security(session, "SPY")
    _position(session, account=account, security=rklb, quantity=100, avg_cost=20, as_of=pf.created_at.date())
    _position(session, account=account, security=spy, quantity=10, avg_cost=450, as_of=pf.created_at.date())
    return pf, account


def test_nav_observation_matches_custodian_total(db_session, flex_ok):
    pf, account = _seed_matching_layer1(db_session)
    _account_snapshot(db_session, account, snap_date=NAV_DATE, nav=9400)  # + 600 cash = 10000
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    (obs,) = result.nav_observations
    assert obs.status == reconciliation.NAV_MATCH
    assert obs.as_of == NAV_DATE
    assert obs.custodian_nav == 10000
    assert obs.metron_nav == 10000 and obs.metron_holdings_nav == 9400 and obs.metron_cash == 600
    assert obs.delta == 0
    assert obs.tolerance == 10.0  # 10 bps of 10000 beats the $1 floor
    assert result.breaks_open == 0


def test_nav_break_is_observed_but_never_persisted_or_alerted(db_session, flex_ok, monkeypatch, caplog):
    """A planted NAV error far below the layer-2 '>100% daily move' guard is caught, and
    observe mode keeps it out of the break store, the alert path and the exit code."""
    alerts = []
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)
    pf, account = _seed_matching_layer1(db_session)
    _account_snapshot(db_session, account, snap_date=NAV_DATE, nav=9000)  # 4% low
    db_session.commit()

    with caplog.at_level("WARNING", logger="api.services.reconciliation"):
        result = reconciliation.reconcile_portfolio(db_session, pf)

    (obs,) = result.nav_observations
    assert obs.status == reconciliation.NAV_BREAK
    assert obs.delta == -400
    assert db_session.query(models.ReconciliationBreak).count() == 0
    assert alerts == []
    assert result.breaks_open == 0 and not result.stale_fetches
    assert any("[nav-observe] BREAK" in r.getMessage() for r in caplog.records)


def test_nav_within_tolerance_is_a_match(db_session, flex_ok):
    pf, account = _seed_matching_layer1(db_session)
    _account_snapshot(db_session, account, snap_date=NAV_DATE, nav=9409.99)  # 9.99 over, tol 10
    db_session.commit()
    (obs,) = reconciliation.reconcile_portfolio(db_session, pf).nav_observations
    assert obs.status == reconciliation.NAV_MATCH


def test_missing_snapshot_for_the_custodian_date_is_unmeasured_not_a_match(db_session, flex_ok, caplog):
    pf, account = _seed_matching_layer1(db_session)
    # A snapshot exists, but for the wrong day: it must not be used.
    _account_snapshot(db_session, account, snap_date=datetime(2026, 6, 4).date(), nav=9400)
    db_session.commit()

    with caplog.at_level("WARNING", logger="api.services.reconciliation"):
        (obs,) = reconciliation.reconcile_portfolio(db_session, pf).nav_observations

    assert obs.status == reconciliation.NAV_UNMEASURED
    assert obs.metron_nav is None and obs.delta is None
    assert "2026-06-05" in obs.reason
    assert any("[nav-observe] UNMEASURED" in r.getMessage() for r in caplog.records)


def test_custodian_without_as_of_is_unmeasured(db_session):
    from portfolio_analytics.ingestion.base import ConnectorSnapshot
    from portfolio_analytics.ingestion.schema import CanonicalAccount

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    account = select_account(db_session, pf, "ibkr_flex", FLEX_ACCT)
    snap = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[
            CanonicalAccount(number=FLEX_ACCT, nav_usd=5000.0, as_of=None),
            CanonicalAccount(number="U-UNKNOWN", nav_usd=1.0, as_of=None),  # not in Metron: skipped
        ],
    )
    (obs,) = reconciliation._observe_account_nav(db_session, pf, snap)
    assert obs.account_id == account.id
    assert obs.status == reconciliation.NAV_UNMEASURED
    assert obs.reason == "custodian reported no as-of date"
    assert obs.tolerance == 5.0


def test_snapshot_with_no_accounts_observes_nothing(db_session):
    from portfolio_analytics.ingestion.base import ConnectorSnapshot

    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    assert reconciliation._observe_account_nav(db_session, pf, ConnectorSnapshot(source="ibkr_flex")) == []


def test_nav_observation_error_never_costs_layer1(db_session, flex_ok, monkeypatch):
    alerts = []
    monkeypatch.setattr(reconciliation, "send_alert", lambda text: alerts.append(text) or True)

    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(reconciliation, "_observe_account_nav", boom)
    pf, account = _seed_matching_layer1(db_session)
    account.cash_balance_usd = 0  # a real layer-1 cash break must still persist and alert
    db_session.commit()

    result = reconciliation.reconcile_portfolio(db_session, pf)

    assert result.breaks_new == 1 and len(alerts) == 1
    assert result.nav_observations == []
    (err,) = result.nav_observe_errors
    assert "db hiccup" in err and "ibkr_flex" in err


def test_reconcile_all_aggregates_nav_observations(db_session, flex_ok):
    pf, account = _seed_matching_layer1(db_session)
    _account_snapshot(db_session, account, snap_date=NAV_DATE, nav=9400)
    db_session.commit()
    total = reconciliation.reconcile_all(db_session)
    assert [o.status for o in total.nav_observations] == [reconciliation.NAV_MATCH]
    assert total.nav_observe_errors == []


def test_cli_reports_nav_observations_without_changing_the_exit_code(session_factory, monkeypatch, caplog):
    from api import maintenance

    obs = reconciliation.NavObservation(
        account_id=__import__("uuid").uuid4(), broker="ibkr_flex", as_of=NAV_DATE, custodian_nav=100.0,
        metron_nav=50.0, metron_holdings_nav=50.0, metron_cash=0.0, tolerance=1.0,
        status=reconciliation.NAV_BREAK,
    )
    canned = reconciliation.ReconcileResult(portfolios_checked=1, nav_observations=[obs])
    monkeypatch.setattr(maintenance, "SessionLocal", session_factory)
    monkeypatch.setattr(maintenance, "create_all", lambda: None)
    monkeypatch.setattr(maintenance.reconciliation, "reconcile_all", lambda session: canned)

    with caplog.at_level("INFO", logger="api.maintenance"):
        assert maintenance.main(["reconcile"]) == 0

    assert any("nav-observe (observe mode): 1 account(s)" in r.getMessage() and "1 break" in r.getMessage()
               for r in caplog.records)


# ── /meta/status reconciliation block (metron-ops#210) ───────────────────────────


def _fetch_row(session, pf, *, last_success_at, broker="ibkr_flex"):
    session.add(
        models.ReconciliationFetchStatus(
            tenant_id=pf.tenant_id,
            portfolio_id=pf.id,
            broker=broker,
            last_success_at=last_success_at,
            first_seen_at=datetime(2026, 10, 1),
        )
    )
    session.flush()


def _open_break(session, pf, *, break_type, break_date, resolved=False):
    account = db_account(session, pf)
    session.add(
        models.ReconciliationBreak(
            tenant_id=pf.tenant_id,
            account_id=account.id,
            break_type=break_type,
            break_key=f"{account.id}:{break_type}:{break_date}",
            break_date=break_date,
            resolved_at=datetime(2026, 10, 20) if resolved else None,
        )
    )
    session.flush()


def db_account(session, pf):
    return session.query(models.Account).filter_by(portfolio_id=pf.id).one()


NOW = datetime(2026, 10, 24, 6, 0, tzinfo=UTC)


def test_status_summary_never_reconciled(db_session):
    s = reconciliation.status_summary(db_session, now=NOW)
    assert s["available"] is True
    assert s["state"] == "never"
    assert s["open_breaks"] == 0 and s["brokers_tracked"] == 0
    assert s["last_successful_fetch_at"] is None
    assert s["nav_check"] == {"mode": "observe", "persisted": False}


def test_status_summary_reconciled_when_fresh_and_clean(db_session):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    _fetch_row(db_session, pf, last_success_at=datetime(2026, 10, 23, 23, 15))
    _open_break(db_session, pf, break_type="cash", break_date=datetime(2026, 10, 2).date(), resolved=True)
    s = reconciliation.status_summary(db_session, now=NOW)
    assert s["state"] == "reconciled"
    assert s["open_breaks"] == 0  # a resolved break is history, not an open count
    assert s["last_successful_fetch_at"] == "2026-10-23T23:15:00+00:00"


def test_status_summary_counts_open_breaks_without_identifiers(db_session):
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    _fetch_row(db_session, pf, last_success_at=datetime(2026, 10, 23, 23, 15))
    _open_break(db_session, pf, break_type="quantity", break_date=datetime(2026, 10, 20).date())
    _open_break(db_session, pf, break_type="quantity", break_date=datetime(2026, 10, 22).date())
    _open_break(db_session, pf, break_type="cash", break_date=datetime(2026, 10, 21).date())
    s = reconciliation.status_summary(db_session, now=NOW)
    assert s["state"] == "breaks"
    assert s["open_breaks"] == 3
    assert s["open_breaks_by_type"] == {"cash": 1, "quantity": 2}
    assert s["oldest_open_break_date"] == "2026-10-20"
    assert str(pf.id) not in repr(s) and FLEX_ACCT not in repr(s)


def test_status_summary_stale_outranks_breaks(db_session):
    """A clean break count from a detector that stopped running is not evidence."""
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    _fetch_row(db_session, pf, last_success_at=NOW.replace(tzinfo=None) - timedelta(days=5))
    _open_break(db_session, pf, break_type="cash", break_date=datetime(2026, 10, 18).date())
    s = reconciliation.status_summary(db_session, now=NOW)
    assert s["state"] == "stale"
    assert s["stale_fetches"] == 1 and s["open_breaks"] == 1


def test_status_endpoint_carries_the_reconciliation_block(client):
    body = client.get("/meta/status").json()
    rec = body["reconciliation"]
    assert rec["available"] is True
    assert rec["state"] == "never"
    assert "not yet deployed" not in repr(rec)


def test_status_survives_a_failing_reconciliation_summary(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(reconciliation, "status_summary", boom)
    r = client.get("/meta/status")
    assert r.status_code == 200
    assert r.json()["reconciliation"] == {"available": False, "error": "RuntimeError"}
