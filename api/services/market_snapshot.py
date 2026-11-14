"""Shared TTL-cached reader for the intraday market-data snapshot.

Both ``intraday.py`` (the Holdings live overlay) and ``indices.py`` (the Markets
strip) consume the SAME S3 artifact, ``market_data/intraday/latest.json``. Before
this module ``intraday`` held its own ~30s process cache while ``indices`` held
none at all, so a single page render could issue two adjacent S3 GetObjects of the
same object and observe up to the producer's ~5-min write cadence of skew between
the strip and the overlay (bounded, and ``session_date`` guards prevent
cross-session mixing — why the drift was a P3, not a correctness bug). Lifting one
cached reader that both modules import collapses that to a single S3 read per TTL
window across both consumers.

The ``reader=`` injection seams in both modules bypass this entirely (they call the
injected reader, never this cache), so tests remain unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import cast

from api.services.spine_artifacts import JsonObject
from portfolio_analytics.prices.source import InputSource, s3_input_source

logger = logging.getLogger(__name__)

INTRADAY_KEY = "market_data/intraday/latest.json"
# Pinned producer version (tests/contracts/metron_intraday_latest.schema.json). Every reader
# of this artifact (indices, intraday overlay) refuses any other version whole at runtime
# (metron-ops-I373); re-pin both together when the producer bumps it.
INTRADAY_SCHEMA_VERSION = 3


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


# Process-level TTL cache for the intraday snapshot S3 read. A single page load fans out to
# ~5–7 endpoints (Holdings + Accounts + Today + intraday-legs + per-security day legs) plus
# the Markets strip, each of which reads the SAME ``latest.json`` artifact — so without this
# the snapshot was fetched from S3 many times per render, serially, dominating the Holdings
# page latency. The producer writes every ~5 min and the snapshot's own staleness window is
# 20 min (``STALE_AFTER_SECONDS`` in each consumer), so a short TTL well under both collapses
# the fan-out to one read while never serving a snapshot materially older than an un-cached
# read would. Freshness is still judged per-call in each consumer against live ``now`` (the
# cache holds the artifact, not a freshness verdict), so a cached-but-aged snapshot still
# correctly reports ``stale``. Mirrors the monotonic-throttle pattern in
# ``data_spine.touch_ui_heartbeat``.
_SNAPSHOT_TTL_S = 30.0
_snapshot_lock = threading.Lock()
_snapshot_cache: tuple[JsonObject | None, InputSource | None] = (None, None)
_snapshot_fetched_monotonic: float = 0.0


def _read_snapshot_s3() -> tuple[JsonObject | None, InputSource | None]:
    import boto3

    bucket = _bucket()
    try:
        obj = cast("JsonObject", boto3.client("s3").get_object(Bucket=bucket, Key=INTRADAY_KEY))  # pyright: ignore[reportUnknownMemberType]  # boto3 ships no types here
        art: object = json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: consumers degrade to EOD / "unavailable", never break the page
        logger.warning("data-spine read failed %s: %s", INTRADAY_KEY, e)
        return None, None
    if art is None:
        return None, None
    # Not narrowed to a dict here: a non-object payload is refused by the reader's admit_schema.
    return cast("JsonObject", art), s3_input_source(bucket, INTRADAY_KEY, obj, art)


def read_cached_artifact() -> tuple[JsonObject | None, InputSource | None]:
    """The cached intraday snapshot and the object version it was read from (metron-ops-I373),
    or ``(None, None)`` on read failure. At most one S3 read per ``_SNAPSHOT_TTL_S`` across all
    consumers in this process; concurrent callers within the window share the cached value, and
    a cache hit carries the lineage of the read that filled it. A failed read is also cached for
    the window so a transient S3 blip during a page load doesn't trigger a retry storm (recovery
    lag <= TTL is acceptable — fail-soft already degrades to EOD close / markets-unavailable)."""
    global _snapshot_cache, _snapshot_fetched_monotonic
    with _snapshot_lock:
        if time.monotonic() - _snapshot_fetched_monotonic < _SNAPSHOT_TTL_S:
            return _snapshot_cache
        _snapshot_cache = _read_snapshot_s3()
        _snapshot_fetched_monotonic = time.monotonic()
        return _snapshot_cache


def read_cached_snapshot() -> JsonObject | None:
    """:func:`read_cached_artifact` without the lineage."""
    return read_cached_artifact()[0]
