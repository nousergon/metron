"""Short-TTL, process-level cache for the data-spine JSON artifacts read on page loads.

The Holdings landing page reads the same daily ``market_data/*`` artifacts from S3 many
times over: the holdings request alone fetched fundamentals, technicals (twice — once for
the metrics, once for the technical rating's EOD fallback), the technical ratings, analyst,
sentiment and security performance, and the watchlist and valuation-medians requests
streamed beside it fetched most of them again — 18 GetObjects per page, each building a
fresh boto3 client and re-parsing the JSON on the single API worker. Nothing about the
artifacts changes at that rate: all but the technical ratings are written once a day, the
ratings every few minutes.

So the module readers' default (S3) path goes through :func:`read_json`, which serves a
parsed artifact for ``TTL_S`` seconds per ``(bucket, key)``. Only a successful read is
kept: a failed read returns ``None`` exactly as before and the next call retries, so a
transient S3 error can never pin a page to "unavailable". The cached dict is shared —
consumers parse it into their own snapshot types and never mutate it. The ``reader=``
injection seams bypass this module entirely, as they always did.

``intraday/latest.json`` keeps its own 30-second cache in ``market_snapshot`` (it is
written every ~5 minutes and judged for staleness per call).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

#: How long a successfully read artifact is served from memory. Well under the fastest
#: producer cadence that goes through here (the intraday technical ratings, ~5 minutes).
TTL_S = 60.0

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def _default_bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _fetch(bucket: str, key: str) -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: every consumer degrades to "unavailable", never breaks a page
        logger.warning("data-spine read failed %s: %s", key, e)
        return None


def read_json(key: str, *, bucket: str | None = None) -> dict | None:
    """The parsed artifact at ``s3://bucket/key`` (or ``None`` on read failure), served
    from memory for ``TTL_S`` seconds after a successful read."""
    ck = (bucket or _default_bucket(), key)
    with _lock:
        hit = _cache.get(ck)
        if hit is not None and time.monotonic() - hit[0] < TTL_S:
            return hit[1]
    art = _fetch(*ck)  # outside the lock: reads of different keys never queue behind each other
    if art is not None:
        with _lock:
            _cache[ck] = (time.monotonic(), art)
    return art


def clear() -> None:
    """Drop every cached artifact — for tests."""
    with _lock:
        _cache.clear()
