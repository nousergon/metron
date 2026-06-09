"""Ingestion orchestrator — run connectors, enforce ownership, merge to silver.

The one place per-account **ownership** is enforced: each account is owned by
exactly one connector (config-declared), so an account that appears in two sources
(e.g. an IBKR account visible to both SnapTrade and Flex) is never double-counted.
Ownership is resolved here, before the silver merge — never inside a connector (it
knows nothing of others) and never in the read adapter (too late; the double-count
is already persisted).

Degradation is **per connector**: a connector that fails (raises, or returns a
snapshot with ``error`` set) is skipped, leaving its last-good silver records intact
— a transient IBKR outage must not blank IBKR's NAV, only leave it stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from portfolio_analytics.ingestion.base import BrokerConnector, ConnectorSnapshot
from portfolio_analytics.ingestion.store import CanonicalStore, load_store, save_bronze, save_store

logger = logging.getLogger(__name__)


@dataclass
class OwnershipPolicy:
    """Resolves which connector owns an account number.

    ``explicit`` maps a connector ``source`` → the account numbers it owns.
    ``default_source`` owns any account no connector explicitly claims (typically the
    aggregator, e.g. SnapTrade, which sees every linked account). This models the
    three cutover modes: snaptrade (default owns IBKR), observe (Flex claims nothing,
    so its IBKR records drop), flex (Flex explicitly owns the IBKR numbers).
    """

    explicit: dict[str, set[str]] = field(default_factory=dict)
    default_source: str | None = None

    def owner_of(self, account_number: str) -> str | None:
        for src, numbers in self.explicit.items():
            if account_number in numbers:
                return src
        return self.default_source


def _filter_owned(snapshot: ConnectorSnapshot, policy: OwnershipPolicy):
    """Drop every record for an account this connector does not own, plus the
    securities only those records referenced (keeps the master clean)."""
    src = snapshot.source
    accounts = [a for a in snapshot.accounts if policy.owner_of(a.number) == src]
    holdings = [h for h in snapshot.holdings if policy.owner_of(h.account_number) == src]
    activities = [a for a in snapshot.activities if policy.owner_of(a.account_number) == src]
    lots = [(n, rg) for (n, rg) in snapshot.realized_lots if policy.owner_of(n) == src]
    referenced = {h.security_id for h in holdings} | {a.security_id for a in activities if a.security_id}
    securities = [s for s in snapshot.securities if s.security_id in referenced]
    return accounts, securities, holdings, activities, lots


def ingest(
    connectors: list[BrokerConnector],
    policy: OwnershipPolicy,
    *,
    store: CanonicalStore | None = None,
    persist: bool = True,
    raw_payloads: dict[str, str | bytes] | None = None,
) -> CanonicalStore:
    """Run each connector, land bronze, merge owned records into the silver store.

    Starts from the persisted silver store (last-good), so a failing connector
    degrades to its prior records. ``raw_payloads`` optionally supplies the raw
    bronze blob per source when a connector exposes it (the connector itself may also
    have landed bronze during ``sync``). Returns the merged store; persists it when
    ``persist`` (default).
    """
    store = store if store is not None else load_store()
    for connector in connectors:
        src = connector.source
        try:
            snapshot = connector.sync()
        except Exception as e:  # noqa: BLE001 — a connector must never take down ingest
            logger.warning("Connector %s raised during sync: %s — keeping last-good", src, e)
            continue
        if snapshot.error:
            logger.warning("Connector %s reported error: %s — keeping last-good", src, snapshot.error)
            continue
        if raw_payloads and src in raw_payloads:
            save_bronze(src, raw_payloads[src])
        accounts, securities, holdings, activities, lots = _filter_owned(snapshot, policy)
        store.merge(src, accounts, securities, holdings, activities, lots)
    if persist:
        save_store(store)
    return store
