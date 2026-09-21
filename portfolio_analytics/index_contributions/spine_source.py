"""Index constituent weight x return decomposition from the alpha-engine-data
**data spine**.

Reads ``market_data/index_contributions/{index}/{date}.json`` (produced by
alpha-engine-config-I11297; the collector also publishes a same-shape
``.../latest.json``). Metron is a pure S3 consumer — no direct market-data fetch.

Fail-soft: a missing artifact for the requested date returns None (never a fabricated
decomposition, and never a silently-substituted different day's data). The
``latest.json`` fallback is used ONLY when it is itself dated for the requested day —
otherwise this returns None and the caller renders "unexplained", not "no drivers".
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

logger = logging.getLogger(__name__)


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


def spine_index_contributions(index: str, as_of: date, *, s3=None) -> dict | None:
    """The raw index-contributions artifact dict for ``index`` on ``as_of``, or None."""
    client = s3 or _s3()
    dated_key = f"market_data/index_contributions/{index}/{as_of.isoformat()}.json"
    art = _read_json(client, dated_key)
    if art is not None:
        return art
    latest_key = f"market_data/index_contributions/{index}/latest.json"
    art = _read_json(client, latest_key)
    if art is not None and art.get("as_of") == as_of.isoformat():
        return art
    return None
