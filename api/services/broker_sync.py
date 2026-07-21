"""Automated re-sync of live broker connections for the daily maintenance job.

Broker-reported positions (``models.Position``) are a point-in-time snapshot, not an
event stream derived from the transaction ledger (see
``persistence._replace_positions``): a real trade at the broker is invisible to Metron
until something re-fetches the snapshot. Before this module, that re-fetch only
happened when a user clicked "Sync IBKR" / "Sync SnapTrade" in the import panel — there
was no scheduled re-sync, so Holdings could silently drift from the real portfolio for
an unbounded time (metron-ops#150: a sold PLTR position still showed its pre-sale
value days later).

This module is the headless counterpart to ``api.routers.portfolios``' interactive
``sync_flex`` / ``import_snaptrade`` routes, reusing the same server-side credentials
(``settings.flex_token``/``flex_query_id``, ``settings.snaptrade_personal``) and the
same ``persistence.persist_snapshot`` bridge. It is called from
``api.maintenance.daily_refresh`` per portfolio, gated on that portfolio having
PREVIOUSLY connected the broker (so a CSV/OFX-only portfolio is never probed for
credentials it doesn't use).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.services import persistence
from portfolio_analytics.broker_io.snaptrade_reader import SnapTradeReader
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.ibkr_flex_connector import IbkrFlexConnector
from portfolio_analytics.ingestion.snaptrade import SnapTradeConnector


def _synced_brokers(session: Session, portfolio: models.Portfolio) -> set[str]:
    """Broker values this portfolio has at least one previously-imported account for —
    the signal that a live connection exists and should be kept fresh automatically.
    A portfolio built purely from CSV/OFX uploads has no live connector and correctly
    yields an empty set here, so it's never probed for broker credentials it doesn't use."""
    rows = session.scalars(
        select(models.Account.broker).where(
            models.Account.tenant_id == portfolio.tenant_id,
            models.Account.portfolio_id == portfolio.id,
        )
    ).all()
    return set(rows)


def fetch_flex_snapshot_for_portfolio(session: Session, portfolio: models.Portfolio) -> ConnectorSnapshot | None:
    """Fetch (but do NOT persist) this portfolio's IBKR Flex-sourced snapshot from the
    deployment's stored token/query id.

    Returns ``None`` (a no-op, not an error) only when this portfolio has never
    connected Flex before. A portfolio WITH Flex-sourced accounts but no stored
    credentials raises — that combination means its positions silently go stale
    (the 2026-07-08 incident: the refresh unit missed the env overlay carrying
    FLEX_TOKEN, so every run logged ``flex_synced=False`` with no warning while
    77 positions froze for weeks). Raises on a real fetch failure too — callers wrap
    this in their own best-effort try/except so a Flex outage never costs the rest
    of the run (price refresh / NAV snapshot, or the reconciliation job's other
    portfolios)."""
    if "ibkr_flex" not in _synced_brokers(session, portfolio):
        return None
    if not (settings.flex_token and settings.flex_query_id):
        raise RuntimeError(
            "portfolio has IBKR Flex accounts but no stored Flex credentials are "
            "configured (FLEX_TOKEN/FLEX_QUERY_ID) — positions will go stale until fixed"
        )
    connector = IbkrFlexConnector(settings.flex_token, settings.flex_query_id, persist_bronze=False)
    snapshot = connector.sync()
    if snapshot.error:
        raise RuntimeError(f"IBKR Flex sync failed: {snapshot.error}")
    return snapshot


def fetch_snaptrade_snapshot_for_portfolio(session: Session, portfolio: models.Portfolio) -> ConnectorSnapshot | None:
    """Fetch (but do NOT persist) this portfolio's SnapTrade-sourced snapshot from the
    operator's linked brokerages, honoring the same per-portfolio connection exclusions
    ``sync_snaptrade_for_portfolio`` applies before persisting.

    Returns ``None`` (a no-op, not an error) only when this portfolio has never synced
    SnapTrade before. A portfolio WITH SnapTrade-sourced accounts on a deploy where
    personal-mode sync is off/unconfigured raises — same silent-staleness class as the
    Flex guard above (positions freeze with no signal). M2's per-user connection-portal
    flow replaces personal mode and reworks this gate. Raises on a real fetch failure
    too — callers wrap this in their own best-effort try/except."""
    if not any(b.startswith("snaptrade") for b in _synced_brokers(session, portfolio)):
        return None
    if not settings.snaptrade_personal:
        raise RuntimeError(
            "portfolio has SnapTrade accounts but personal-mode SnapTrade sync is off "
            "(SNAPTRADE_PERSONAL) — positions will go stale until fixed"
        )
    try:
        reader = SnapTradeReader.from_env()
    except KeyError as e:
        raise RuntimeError(f"SnapTrade not configured — missing {e}") from e
    snapshot = SnapTradeConnector(reader).sync()
    if snapshot.error:
        raise RuntimeError(f"SnapTrade sync failed: {snapshot.error}")
    excluded_ids = persistence.snaptrade_excluded_ids(session, portfolio.tenant_id, portfolio.id)
    if excluded_ids:
        accounts = reader.get_accounts()
        excluded_numbers = {
            a.get("number") for a in accounts if a.get("brokerage_authorization") in excluded_ids
        }
        snapshot.accounts = [a for a in snapshot.accounts if a.number not in excluded_numbers]
        snapshot.holdings = [h for h in snapshot.holdings if h.account_number not in excluded_numbers]
        snapshot.activities = [a for a in snapshot.activities if a.account_number not in excluded_numbers]
    return snapshot


def sync_flex_for_portfolio(session: Session, portfolio: models.Portfolio) -> persistence.PersistResult | None:
    """Re-sync this portfolio's IBKR Flex-sourced accounts from the deployment's stored
    token/query id — the automated counterpart to ``POST /sync/flex``. See
    ``fetch_flex_snapshot_for_portfolio`` for the no-op / raise conditions."""
    snapshot = fetch_flex_snapshot_for_portfolio(session, portfolio)
    if snapshot is None:
        return None
    return persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=snapshot
    )


def sync_snaptrade_for_portfolio(session: Session, portfolio: models.Portfolio) -> persistence.PersistResult | None:
    """Re-sync this portfolio's SnapTrade-sourced accounts from the operator's linked
    brokerages — the automated counterpart to ``POST /import/snaptrade``. See
    ``fetch_snaptrade_snapshot_for_portfolio`` for the no-op / raise conditions."""
    snapshot = fetch_snaptrade_snapshot_for_portfolio(session, portfolio)
    if snapshot is None:
        return None
    return persistence.persist_snapshot(
        session, tenant_id=portfolio.tenant_id, portfolio_id=portfolio.id, snapshot=snapshot
    )
