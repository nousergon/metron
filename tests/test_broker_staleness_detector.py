"""Headless broker-position staleness detector (metron-ops#260).

The Holdings view has warned about stale share counts since metron-ops#150, but nothing
headless did: when the SnapTrade credential path broke on 2026-07-26, ``daily-refresh``
logged ``snaptrade_synced=False`` at INFO and exited 0 for nine consecutive days while
four accounts' positions froze at 2026-07-25. These tests pin the counterweight —
detection, an operator alert, and a non-zero exit — and they exercise the SHIPPED
functions (``broker_sync.stale_broker_accounts`` / ``maintenance.report_broker_staleness``
/ ``maintenance.main``), not a re-implementation of the predicate.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from api import maintenance
from api.db import models
from api.services import broker_sync
from api.services.security_perf import STALE_AFTER_SESSIONS

# A Mon–Fri run of sessions with no NYSE holiday in it, so "sessions behind" equals
# "weekdays behind" and the fixtures stay readable.
TODAY = date(2026, 8, 6)          # Thursday
PRIOR_SESSION = date(2026, 8, 5)  # Wednesday — 1 session behind, NOT stale
TWO_BEHIND = date(2026, 8, 4)     # Tuesday   — 2 sessions behind, stale


def _seed_account(session, *, broker: str, as_of: date | None, name: str = "Acct") -> models.Account:
    """One account of ``broker`` holding a single position dated ``as_of`` (or none)."""
    tenant = models.Tenant(name=f"t-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker=broker,
        external_id=f"X{uuid.uuid4().hex[:6]}", name=name, currency="USD",
    )
    session.add(acct)
    sec = models.Security(symbol=f"S{uuid.uuid4().hex[:4].upper()}", currency="USD")
    session.add(sec)
    session.flush()
    if as_of is not None:
        session.add(
            models.Position(
                tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                quantity=10, avg_cost=1, currency="USD", as_of=as_of,
            )
        )
    session.commit()
    return acct


def test_threshold_is_the_same_one_the_holdings_badge_uses():
    """One threshold, one definition of stale — a screen that warns while nothing pages
    is the failure this detector exists to close."""
    assert STALE_AFTER_SESSIONS == 2


@pytest.mark.parametrize("broker", ["ibkr_flex", "snaptrade"])
def test_flags_a_snapshot_account_two_sessions_behind(db_session, broker):
    _seed_account(db_session, broker=broker, as_of=TWO_BEHIND, name="Frozen")
    stale = broker_sync.stale_broker_accounts(db_session, today=TODAY)
    assert [s.account_name for s in stale] == ["Frozen"]
    assert stale[0].as_of == TWO_BEHIND
    assert stale[0].sessions_behind == 2
    assert stale[0].broker == broker


def test_prior_session_is_not_stale(db_session):
    """One session behind is the NORMAL state before today's sync lands — flagging it
    would page every morning and train the operator to ignore the alert."""
    _seed_account(db_session, broker="snaptrade", as_of=PRIOR_SESSION)
    assert broker_sync.stale_broker_accounts(db_session, today=TODAY) == []


def test_ledger_sourced_accounts_are_never_flagged(db_session):
    """CSV/OFX positions come from an uploaded ledger, so "the sync hasn't run" is not a
    failure mode for them, however old the rows are."""
    _seed_account(db_session, broker="csv", as_of=date(2026, 1, 2))
    _seed_account(db_session, broker="ofx", as_of=date(2026, 1, 2))
    assert broker_sync.stale_broker_accounts(db_session, today=TODAY) == []


def test_max_as_of_is_a_date_not_a_string(db_session):
    """SQLite stores dates as TEXT; a ``max()`` that leaks the raw string would blow up
    in ``sessions_behind`` (or worse, compare lexically). Pin the type."""
    _seed_account(db_session, broker="ibkr_flex", as_of=TWO_BEHIND)
    stale = broker_sync.stale_broker_accounts(db_session, today=TODAY)
    assert isinstance(stale[0].as_of, date)


def test_only_the_latest_position_date_counts(db_session):
    """An account re-synced today still holds older rows for securities it no longer
    reports; freshness is the MAX, not the min."""
    acct = _seed_account(db_session, broker="snaptrade", as_of=TWO_BEHIND)
    sec = models.Security(symbol="FRESH", currency="USD")
    db_session.add(sec)
    db_session.flush()
    db_session.add(
        models.Position(
            tenant_id=acct.tenant_id, account_id=acct.id, security_id=sec.id,
            quantity=1, avg_cost=1, currency="USD", as_of=TODAY,
        )
    )
    db_session.commit()
    assert broker_sync.stale_broker_accounts(db_session, today=TODAY) == []


def test_report_alerts_and_returns_the_stale_accounts(db_session, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        maintenance.alerting, "send_alert",
        lambda text, **kw: sent.append((text, kw.get("severity", ""))) or True,
    )
    _seed_account(db_session, broker="snaptrade", as_of=TWO_BEHIND, name="BrokerageLink")
    stale = maintenance.report_broker_staleness(db_session, today=TODAY)
    assert len(stale) == 1
    assert len(sent) == 1
    text, severity = sent[0]
    assert "BrokerageLink" in text and "2026-08-04" in text
    # error is the tier that pushes; a silent tier here would reproduce the nine-day
    # blind spot with extra steps.
    assert severity == "error"


def test_report_is_silent_when_everything_is_current(db_session, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(maintenance.alerting, "send_alert", lambda text, **kw: sent.append(text) or True)
    _seed_account(db_session, broker="ibkr_flex", as_of=TODAY)
    assert maintenance.report_broker_staleness(db_session, today=TODAY) == []
    assert sent == []


@pytest.mark.parametrize("cmd", ["daily-refresh", "flex-sync"])
def test_cli_exits_non_zero_when_positions_are_stale(db_session, session_factory, monkeypatch, cmd):
    """A run that refreshed prices but could not advance share counts is a FAILED run —
    exit 0 would leave the systemd unit green while the data quietly rots."""
    monkeypatch.setattr(maintenance, "SessionLocal", session_factory)
    monkeypatch.setattr(maintenance, "create_all", lambda: None)
    monkeypatch.setattr(maintenance, "daily_refresh", lambda session, **kw: maintenance.RefreshResult(0, 0, 0, 0))
    monkeypatch.setattr(maintenance, "flex_sync_all", lambda session: 0)
    monkeypatch.setattr(maintenance.alerting, "send_alert", lambda text, **kw: True)
    monkeypatch.setattr(maintenance, "report_broker_staleness", lambda session, **kw: ["stale"])
    assert maintenance.main([cmd]) == 1


@pytest.mark.parametrize("cmd", ["daily-refresh", "flex-sync"])
def test_cli_exits_zero_when_positions_are_current(db_session, session_factory, monkeypatch, cmd):
    monkeypatch.setattr(maintenance, "SessionLocal", session_factory)
    monkeypatch.setattr(maintenance, "create_all", lambda: None)
    monkeypatch.setattr(maintenance, "daily_refresh", lambda session, **kw: maintenance.RefreshResult(0, 0, 0, 0))
    monkeypatch.setattr(maintenance, "flex_sync_all", lambda session: 0)
    monkeypatch.setattr(maintenance, "report_broker_staleness", lambda session, **kw: [])
    assert maintenance.main([cmd]) == 0
