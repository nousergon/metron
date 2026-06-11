"""GICS sectors + benchmark sector weights from the alpha-engine-data **data spine**.

`alpha-engine-data` is the single market/reference-data ground truth for the Nous Ergon
system — Metron reads sectors from its S3 artifact and makes no direct classification
fetch. Reads `market_data/sectors/latest.json` (produced by
alpha-engine-data/collectors/metron_market_data.py). Keyed by `yf_symbol` — the consumer
resolves symbol→yf_symbol before querying, mirroring the price source.

Fail-soft: a missing artifact / unclassified symbol → omitted (the caller leaves
`sector = NULL`, counted as a coverage gap, never a guessed sector). Bucket from
``MARKET_DATA_BUCKET`` (default ``alpha-engine-research``).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

SECTORS_LATEST_KEY = "market_data/sectors/latest.json"


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _s3():
    import boto3
    return boto3.client("s3")


def _read_json(s3, key: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=_bucket(), Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:  # missing object / no creds / parse error
        logger.warning("data-spine read failed %s: %s", key, e)
        return None


def spine_sectors(yf_symbols: list[str], *, s3=None) -> dict[str, str]:
    """GICS sector per yf_symbol from the spine. Unclassified symbols omitted."""
    art = _read_json(s3 or _s3(), SECTORS_LATEST_KEY) or {}
    sectors = art.get("sectors", {})
    return {sym: sectors[sym] for sym in yf_symbols if sectors.get(sym)}


def spine_benchmark_sector_weights(*, s3=None) -> dict[str, float]:
    """SPY's GICS sector weights (canonical label → fraction) from the spine. ``{}`` if
    absent → the attribution degrades to not-computable WITH a reason, never fabricated."""
    art = _read_json(s3 or _s3(), SECTORS_LATEST_KEY) or {}
    weights = art.get("spy_sector_weights", {})
    return {k: float(v) for k, v in weights.items()}
