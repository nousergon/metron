"""NE factor profiles from the data spine — substrate for SOTA attractiveness (metron-ops#106).

Reads ``factors/profiles/latest.json`` (produced weekly by crucible-research's
``factor_scoring.compute_and_write_factor_profiles`` over the SP1500 scanner universe).
Metron is a pure S3 consumer: missing artifact / absent symbol → omitted, never fabricated.
The source is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

FACTOR_PROFILES_KEY = "factors/profiles/latest.json"
PILLAR_WEIGHTS_KEY = "config/factor_attractiveness_weights.json"

_CACHE_TTL_S = 3600.0  # 1 hour; profiles update weekly
_profiles_cache: dict[str, object | None] = {}  # None = missing, else FactorProfilesSnapshot
_profiles_cache_time: float = 0.0
_weights_cache: dict[str, object | None] = {}  # None = missing, else dict
_weights_cache_time: float = 0.0


@dataclass
class FactorProfilesSnapshot:
    as_of: date | None
    by_ticker: dict[str, dict]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=FACTOR_PROFILES_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:
        logger.warning("data-spine read failed %s: %s", FACTOR_PROFILES_KEY, e)
        return None


def _default_weights_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=PILLAR_WEIGHTS_KEY)
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def load_factor_profiles(*, reader=None) -> FactorProfilesSnapshot:
    """Load the full scanner-universe factor profiles artifact, cached for 1 hour.

    When a custom reader is supplied (tests), bypass the cache entirely."""
    if reader is not None:
        # Test path: always read fresh.
        raw = reader()
    else:
        # Production path: check module-level cache first.
        global _profiles_cache, _profiles_cache_time
        now = time.time()
        if now - _profiles_cache_time < _CACHE_TTL_S and "" in _profiles_cache:
            cached = _profiles_cache[""]
            if cached is None or isinstance(cached, FactorProfilesSnapshot):
                return cached or FactorProfilesSnapshot(as_of=None, by_ticker={})

        raw = _default_reader()
        now = time.time()
        _profiles_cache_time = now

    if not isinstance(raw, dict):
        result = FactorProfilesSnapshot(as_of=None, by_ticker={})
    else:
        # Artifact is a flat {ticker: profile} map; tolerate a wrapped envelope if one appears.
        if "by_ticker" in raw and isinstance(raw["by_ticker"], dict):
            by_ticker = raw["by_ticker"]
            as_of = raw.get("as_of")
        else:
            by_ticker = {k: v for k, v in raw.items() if isinstance(v, dict)}
            as_of = None
        parsed_as_of = None
        if isinstance(as_of, str):
            try:
                parsed_as_of = date.fromisoformat(as_of[:10])
            except ValueError:
                parsed_as_of = None
        result = FactorProfilesSnapshot(as_of=parsed_as_of, by_ticker=by_ticker)

    if reader is None:
        _profiles_cache[""] = result
    return result


def load_pillar_weights(*, reader=None) -> dict[str, float] | None:
    """Optional private tuned weights from ``config/factor_attractiveness_weights.json``, cached for 1 hour.

    When a custom reader is supplied (tests), bypass the cache entirely."""
    if reader is not None:
        # Test path: always read fresh.
        raw = reader()
    else:
        # Production path: check module-level cache first.
        global _weights_cache, _weights_cache_time
        now = time.time()
        if now - _weights_cache_time < _CACHE_TTL_S and "" in _weights_cache:
            return _weights_cache[""]

        raw = _default_weights_reader()
        now = time.time()
        _weights_cache_time = now

    if not isinstance(raw, dict):
        result = None
    else:
        weights = raw.get("weights", raw)
        if not isinstance(weights, dict):
            result = None
        else:
            result = {str(k): float(v) for k, v in weights.items() if v is not None}

    if reader is None:
        _weights_cache[""] = result
    return result
