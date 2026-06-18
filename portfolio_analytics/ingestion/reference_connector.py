"""Reference-rate connector — the illustrative "Reference Rate" showcase portfolio.

A ``BrokerConnector`` whose "broker" is an S3 contract artifact
(``metron/reference_rate.json``) published by the engine's EOD pipeline, rather than a
live brokerage API. It is a **snapshot source** (the artifact reports authoritative
current positions, exactly like IBKR Flex), so it normalizes into the same canonical
schema and flows through the one ``persist_snapshot`` bridge — Crucible is just another
connector, not a special path.

The artifact is illustrative-only and carries no strategy edge (see the producer,
``executor/reference_rate.py`` in the engine repo). This connector maps only its
disclosed surface: positions → ``CanonicalHolding`` + ``CanonicalSecurity``, NAV →
``CanonicalAccount``. The NAV-vs-SPY ``nav_history`` is a portfolio-performance series,
not a connector record — the seeding service (``api.services.demo``) consumes it
directly into ``NavSnapshot`` rows.

Fail-soft like every connector: a missing/unreadable artifact returns a snapshot with
``error`` set so ingest keeps last-good rather than blanking the showcase.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    ASSET_EQUITY,
    CanonicalAccount,
    CanonicalHolding,
    CanonicalSecurity,
    synth_security_id,
)

logger = logging.getLogger(__name__)

SOURCE = "reference"
REFERENCE_RATE_KEY = "metron/reference_rate.json"
# The single illustrative account the showcase rolls up into.
ACCOUNT_NUMBER = "reference"
ACCOUNT_LABEL = "Reference Rate"


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def read_reference_artifact() -> dict[str, Any] | None:
    """Read + parse ``metron/reference_rate.json`` from the data-spine bucket.

    Returns ``None`` on any failure (fail-soft), mirroring ``prices.spine_source``.
    """
    import boto3

    s3 = boto3.client("s3")
    bucket = _bucket()
    try:
        obj = s3.get_object(Bucket=bucket, Key=REFERENCE_RATE_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # missing object / no creds / parse error
        logger.warning("reference-rate read failed s3://%s/%s: %s", bucket, REFERENCE_RATE_KEY, e)
        return None


def artifact_to_snapshot(artifact: dict[str, Any]) -> ConnectorSnapshot:
    """Map a parsed reference-rate artifact into a canonical snapshot (pure).

    One ``CanonicalAccount`` (NAV from the artifact; cash is the reconciling plug
    ``nav − Σ market_value``), and one ``CanonicalHolding`` + ``CanonicalSecurity`` per
    position. No activities — a snapshot source carries current holdings only.
    """
    positions = artifact.get("positions") or []
    nav = (artifact.get("account") or {}).get("net_liquidation")
    base_ccy = artifact.get("base_currency") or "USD"

    holdings: list[CanonicalHolding] = []
    securities: list[CanonicalSecurity] = []
    positions_value = 0.0
    for pos in positions:
        ticker = (pos.get("ticker") or "").strip()
        qty = pos.get("shares")
        if not ticker or not qty:
            continue
        avg_cost = pos.get("avg_cost") or 0.0
        mv = pos.get("market_value") or 0.0
        positions_value += mv
        sid = synth_security_id(ticker, base_ccy)
        holdings.append(
            CanonicalHolding(
                account_number=ACCOUNT_NUMBER,
                security_id=sid,
                quantity=qty,
                cost_basis=abs(qty * avg_cost),
                avg_cost=avg_cost,
                market_value_local=mv,
                currency=base_ccy,
                source=SOURCE,
            )
        )
        securities.append(
            CanonicalSecurity(
                security_id=sid,
                ticker=ticker,
                currency=base_ccy,
                asset_type=ASSET_EQUITY,
            )
        )

    cash = (nav - positions_value) if nav is not None else 0.0
    account = CanonicalAccount(
        number=ACCOUNT_NUMBER,
        label=ACCOUNT_LABEL,
        institution=ACCOUNT_LABEL,
        tax_treatment="taxable",
        nav_usd=nav or 0.0,
        cash_usd=cash,
        currency=base_ccy,
        source=SOURCE,
        account_id=ACCOUNT_NUMBER,
        name=ACCOUNT_LABEL,
    )
    return ConnectorSnapshot(
        source=SOURCE,
        accounts=[account],
        securities=securities,
        holdings=holdings,
    )


class ReferenceRateConnector:
    """``BrokerConnector`` over the reference-rate S3 artifact.

    ``reader`` is injectable for tests (returns the parsed artifact dict directly);
    the default reads from S3 via ``read_reference_artifact``.
    """

    source = SOURCE

    def __init__(self, reader: Callable[[], dict[str, Any] | None] | None = None):
        self._reader = reader or read_reference_artifact

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        try:
            artifact = self._reader()
        except Exception as e:  # noqa: BLE001 — degrade to last-good, never crash ingest
            logger.warning("reference-rate sync failed: %s", e)
            return ConnectorSnapshot(source=SOURCE, error=str(e))
        if not artifact:
            return ConnectorSnapshot(source=SOURCE, error="reference-rate artifact unavailable")
        return artifact_to_snapshot(artifact)
