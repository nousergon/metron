"""Broker-agnostic canonical (FDX) ingestion layer.

The FDX-shaped schema, the bronze/silver canonical store, the ``BrokerConnector``
protocol, the IBKR-Flex and SnapTrade connectors, the ``ingest`` orchestrator,
and the ``CanonicalReader`` drop-in read adapter.
"""

# NOTE: the ``ingest`` *function* is deliberately NOT re-exported here — it would
# shadow the ``portfolio_analytics.ingestion.ingest`` *submodule* (breaking
# attribute access / monkeypatch targets against it). Import it explicitly via
# ``from portfolio_analytics.ingestion.ingest import ingest``.
from portfolio_analytics.ingestion.base import BrokerConnector, ConnectorSnapshot
from portfolio_analytics.ingestion.ibkr_flex_connector import IbkrFlexConnector
from portfolio_analytics.ingestion.ingest import OwnershipPolicy
from portfolio_analytics.ingestion.reader_adapter import CanonicalReader
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
    activity_key,
    lot_key,
    synth_security_id,
)
from portfolio_analytics.ingestion.snaptrade import SnapTradeConnector
from portfolio_analytics.ingestion.store import (
    CanonicalStore,
    load_store,
    save_bronze,
    save_store,
)

__all__ = [
    "BrokerConnector",
    "ConnectorSnapshot",
    "IbkrFlexConnector",
    "OwnershipPolicy",
    "CanonicalReader",
    "CanonicalAccount",
    "CanonicalActivity",
    "CanonicalHolding",
    "CanonicalSecurity",
    "activity_key",
    "lot_key",
    "synth_security_id",
    "SnapTradeConnector",
    "CanonicalStore",
    "load_store",
    "save_bronze",
    "save_store",
]
