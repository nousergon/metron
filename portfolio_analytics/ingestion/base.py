"""Connector protocol + the snapshot a connector returns.

A connector's only job: fetch its broker's data and normalize it into canonical
records. Ownership, dedup, persistence, and degradation are the ingestion layer's
job (``connectors.ingest``) — a connector knows nothing about other connectors.

The protocol is **Singer/Airbyte-shaped**: ``sync(state)`` takes an opaque cursor
and the snapshot echoes one back, so an incremental connector (e.g. IBKR Flex
pulling only since the last close date) slots in with no change to the contract.
v1 connectors return ``FULL_REFRESH`` snapshots and an empty state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from portfolio_analytics.domain.ledger import RealizedGain
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalOpenLot,
    CanonicalSecurity,
)

SYNC_FULL_REFRESH = "FULL_REFRESH"
SYNC_INCREMENTAL = "INCREMENTAL"

# Sources whose broker-reported position snapshot is authoritative for CURRENT
# holdings — including when it is EMPTY (an account that sold everything has
# activities but zero position rows, and is still snapshot-sourced). Their
# activity history exists for realized-gain/dividend reporting and may start
# mid-position (broker history depth), so it must never be replayed into
# current holdings. A new snapshot connector MUST add its source here.
SNAPSHOT_SOURCES = frozenset({"ibkr_flex", "snaptrade", "reference"})


@dataclass
class ConnectorSnapshot:
    """One connector's normalized output for a single sync run.

    ``realized_lots`` are ``(account_number, RealizedGain)`` pairs (immutable closed
    lots, append-only downstream). ``state`` is the Singer-style opaque cursor echoed
    into the next ``sync(state=...)``. ``error`` is set (and the record lists left
    empty) when the live fetch failed, so ingest degrades to last-good rather than
    blanking the source.

    ``holdings_complete_for`` / ``open_lots_complete_for`` are the snapshot's explicit
    replacement scope (metron-ops#382): the account numbers whose CURRENT holdings /
    open lots this snapshot states in full. An account listed there with no rows is
    authoritatively EMPTY, and persistence clears its stored rows. ``None`` declares
    nothing, so only accounts that carry rows are replaced and an account with none is
    preserved: a connector that cannot tell "sold everything" from "this account's
    fetch failed" or "this feed has no lot detail" must leave these ``None`` rather than
    let an absence read as empty. Honored only for a successful ``FULL_REFRESH``.
    """

    source: str
    accounts: list[CanonicalAccount] = field(default_factory=list)
    securities: list[CanonicalSecurity] = field(default_factory=list)
    holdings: list[CanonicalHolding] = field(default_factory=list)
    open_lots: list[CanonicalOpenLot] = field(default_factory=list)  # lot-level open positions
    activities: list[CanonicalActivity] = field(default_factory=list)
    realized_lots: list[tuple[str, RealizedGain]] = field(default_factory=list)
    state: dict = field(default_factory=dict)
    sync_mode: str = SYNC_FULL_REFRESH
    error: str | None = None
    holdings_complete_for: frozenset[str] | None = None
    open_lots_complete_for: frozenset[str] | None = None


@runtime_checkable
class BrokerConnector(Protocol):
    """A broker data source. Implementations: ``connectors.snaptrade``,
    ``connectors.ibkr_flex_connector``."""

    source: str

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        """Fetch + normalize. MUST NOT raise on a transient fetch failure — return a
        snapshot with ``error`` set and empty record lists so ingest keeps last-good."""
