"""Next-earnings dates from the alpha-engine-data **data spine**.

`alpha-engine-data` is the single market/reference-data ground truth for the Nous Ergon
system — Metron reads earnings dates from its S3 artifact and makes no direct fetch.
Reads `market_data/earnings/latest.json` (produced by
alpha-engine-data/collectors/metron_market_data.py), keyed by `yf_symbol` (the consumer
resolves symbol→yf_symbol before querying).

Fail-soft: a missing artifact / undated symbol → omitted (no calendar event, never an
invented date). Bucket from ``MARKET_DATA_BUCKET`` (default ``alpha-engine-research``).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)

EARNINGS_LATEST_KEY = "market_data/earnings/latest.json"


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _s3():
    import boto3
    return boto3.client("s3")


def _read_json(s3, key: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=_bucket(), Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:
        logger.warning("data-spine read failed %s: %s", key, e)
        return None


def spine_earnings_dates(yf_symbols: list[str], *, s3=None) -> dict[str, date]:
    """Next earnings date per yf_symbol from the spine. Undated symbols omitted."""
    art = _read_json(s3 or _s3(), EARNINGS_LATEST_KEY) or {}
    earnings = art.get("earnings", {})
    out: dict[str, date] = {}
    for sym in yf_symbols:
        raw = earnings.get(sym)
        if not raw:
            continue
        try:
            out[sym] = date.fromisoformat(str(raw))
        except ValueError:
            continue
    return out
