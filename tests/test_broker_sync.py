"""Automated broker re-sync for daily_refresh (metron-ops#150) — the headless
counterpart to the interactive "Sync IBKR" / "Sync SnapTrade" routes.

Reuses each connector's own recorded fixtures (the Flex XML fixture from
tests/test_ibkr_flex_connector, the fake SnapTrade reader from
tests/test_connectors_snaptrade) so the real connectors build the snapshot; only the
network boundary is mocked. Pure, no network.
"""

from __future__ import annotations

import pytest

from api.db import models
from api.services import analytics, broker_sync
from tests.test_connectors_snaptrade import _FakeReader
from tests.test_ibkr_flex_connector import STATEMENT

FLEX_ACCT = "U33333333"  # matches the connector test fixture


def _seed_portfolio(session, *, broker: str | None):
    """A portfolio with one account of ``broker`` (or none, for a CSV-only portfolio)."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    if broker:
        session.add(
            models.Account(
                tenant_id=tenant.id, portfolio_id=pf.id, broker=broker,
                external_id=FLEX_ACCT if broker == "ibkr_flex" else "U001", currency="USD",
            )
        )
    session.commit()
    return pf


# ── IBKR Flex ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def flex_ok(monkeypatch):
    """Make the real connector parse the fixture instead of hitting IBKR."""
    monkeypatch.setattr(
        "portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml",
        lambda *a, **k: STATEMENT,
    )


def test_sync_flex_raises_when_connected_but_credentials_missing(db_session, monkeypatch, flex_ok):
    """Fail-loud (2026-07-08 incident): a Flex-connected portfolio with no stored
    credentials means positions silently freeze — that's a WARN-surfaced error via
    daily_refresh's best-effort wrapper, never a silent no-op."""
    monkeypatch.setattr(broker_sync.settings, "flex_token", "")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "")
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    with pytest.raises(RuntimeError, match="no stored Flex credentials"):
        broker_sync.sync_flex_for_portfolio(db_session, pf)
    assert db_session.query(models.Position).count() == 0


def test_sync_flex_noop_without_credentials_when_never_connected(db_session, monkeypatch, flex_ok):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "")
    pf = _seed_portfolio(db_session, broker="csv")
    assert broker_sync.sync_flex_for_portfolio(db_session, pf) is None
    assert db_session.query(models.Position).count() == 0


def test_sync_flex_noop_when_portfolio_never_connected_flex(db_session, monkeypatch, flex_ok):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")
    pf = _seed_portfolio(db_session, broker="csv")  # only a CSV account, never connected Flex
    assert broker_sync.sync_flex_for_portfolio(db_session, pf) is None
    assert db_session.query(models.Position).count() == 0


def test_sync_flex_persists_positions_when_connected(db_session, monkeypatch, flex_ok):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    result = broker_sync.sync_flex_for_portfolio(db_session, pf)
    assert result is not None
    assert result.positions_imported > 0
    assert db_session.query(models.Position).count() == result.positions_imported


def test_sync_flex_raises_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "flex_token", "tok")
    monkeypatch.setattr(broker_sync.settings, "flex_query_id", "qid")

    def _boom(*a, **k):
        raise RuntimeError("IBKR unreachable")

    monkeypatch.setattr("portfolio_analytics.ingestion.ibkr_flex_connector.fetch_flex_xml", _boom)
    pf = _seed_portfolio(db_session, broker="ibkr_flex")
    with pytest.raises(RuntimeError, match="IBKR Flex sync failed"):
        broker_sync.sync_flex_for_portfolio(db_session, pf)


# ── SnapTrade ──────────────────────────────────────────────────────────────────
def test_sync_snaptrade_raises_when_connected_but_disabled(db_session, monkeypatch):
    """Same fail-loud class as Flex: SnapTrade accounts exist but personal-mode sync is
    off — silent staleness, so it must surface as an error, not a no-op."""
    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", False)
    pf = _seed_portfolio(db_session, broker="snaptrade")
    with pytest.raises(RuntimeError, match="personal-mode SnapTrade sync is off"):
        broker_sync.sync_snaptrade_for_portfolio(db_session, pf)


def test_sync_snaptrade_noop_when_disabled_and_never_connected(db_session, monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", False)
    pf = _seed_portfolio(db_session, broker="csv")
    assert broker_sync.sync_snaptrade_for_portfolio(db_session, pf) is None


def test_sync_snaptrade_noop_when_portfolio_never_connected(db_session, monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    pf = _seed_portfolio(db_session, broker="csv")
    assert broker_sync.sync_snaptrade_for_portfolio(db_session, pf) is None


def _reader_source(reader_factory):
    """A stand-in for ``SnapTradeReader`` exposing only the ``from_env()`` entry point
    ``broker_sync`` calls — rebinding the module-level name (not mutating the shared
    class) keeps each test isolated."""

    class _Source:
        @staticmethod
        def from_env():
            return reader_factory()

    return _Source


def test_sync_snaptrade_persists_positions_when_connected(db_session, monkeypatch):
    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    monkeypatch.setattr(broker_sync, "SnapTradeReader", _reader_source(_FakeReader))
    pf = _seed_portfolio(db_session, broker="snaptrade")
    result = broker_sync.sync_snaptrade_for_portfolio(db_session, pf)
    assert result is not None
    assert result.positions_imported == 3  # AAPL, RKLB, VOO from the fixture
    held = analytics.holdings(db_session, pf.tenant_id, pf.id)
    assert {h.ticker for h in held} == {"AAPL", "RKLB", "VOO"}


def test_sync_snaptrade_honors_connection_exclusions(db_session, monkeypatch):
    class _AuthReader(_FakeReader):
        def get_accounts(self):
            accounts = [dict(a) for a in super().get_accounts()]
            for a in accounts:
                a["brokerage_authorization"] = "auth-ibkr"
            return accounts

    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    monkeypatch.setattr(broker_sync, "SnapTradeReader", _reader_source(_AuthReader))
    pf = _seed_portfolio(db_session, broker="snaptrade")
    db_session.add(
        models.InvestorPreferences(
            tenant_id=pf.tenant_id, portfolio_id=pf.id, snaptrade_excluded_connections="auth-ibkr",
        )
    )
    db_session.commit()
    result = broker_sync.sync_snaptrade_for_portfolio(db_session, pf)
    assert result is not None
    assert result.positions_imported == 0  # the excluded connection's accounts are dropped


def test_sync_snaptrade_raises_on_missing_env_credentials(db_session, monkeypatch):
    def _raise():
        raise KeyError("SNAPTRADE_CLIENT_ID")

    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    monkeypatch.setattr(broker_sync, "SnapTradeReader", _reader_source(_raise))
    pf = _seed_portfolio(db_session, broker="snaptrade")
    with pytest.raises(RuntimeError, match="SNAPTRADE_CLIENT_ID"):
        broker_sync.sync_snaptrade_for_portfolio(db_session, pf)


def test_sync_snaptrade_raises_on_fetch_failure(db_session, monkeypatch):
    class _BoomReader:
        def get_accounts(self):
            raise RuntimeError("token expired")

    monkeypatch.setattr(broker_sync.settings, "snaptrade_personal", True)
    monkeypatch.setattr(broker_sync, "SnapTradeReader", _reader_source(_BoomReader))
    pf = _seed_portfolio(db_session, broker="snaptrade")
    with pytest.raises(RuntimeError, match="token expired"):
        broker_sync.sync_snaptrade_for_portfolio(db_session, pf)
