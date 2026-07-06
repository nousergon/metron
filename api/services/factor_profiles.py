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
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

FACTOR_PROFILES_KEY = "factors/profiles/latest.json"
PILLAR_WEIGHTS_KEY = "config/factor_attractiveness_weights.json"


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
    """Load the full scanner-universe factor profiles artifact."""
    raw = (reader or _default_reader)()
    if not isinstance(raw, dict):
        return FactorProfilesSnapshot(as_of=None, by_ticker={})
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
    return FactorProfilesSnapshot(as_of=parsed_as_of, by_ticker=by_ticker)


def load_pillar_weights(*, reader=None) -> dict[str, float] | None:
    """Optional private tuned weights from ``config/factor_attractiveness_weights.json``."""
    raw = (reader or _default_weights_reader)()
    if not isinstance(raw, dict):
        return None
    weights = raw.get("weights", raw)
    if not isinstance(weights, dict):
        return None
    return {str(k): float(v) for k, v in weights.items() if v is not None}
