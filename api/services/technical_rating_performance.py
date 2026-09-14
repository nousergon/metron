"""Technical rating track record (metron-ops#298, Brian ruling 2026-09-14) — the technical
rating's realized near-term forward performance, read from the nousergon-data producer
artifact ``market_data/technicals/rating_performance.json`` (schema v1).

Mirrors ``technical_rating.py`` (module-level KEY, fail-soft reader, injectable ``reader``,
dataclass + ``load_*``). Metron is a pure S3 consumer: a missing artifact, a schema-version
mismatch, or a malformed field is treated as the WHOLE artifact being absent — never
partially fabricated. The artifact does not exist in S3 yet at the time this consumer ships
(concurrent sibling producer issue) — every read degrades to "no track record", exactly like
every other absent data-spine artifact.

MEASURED 2026-09-14 (backfill grade): the rating carries **no predictive edge at 1-20 days**
(IC ~= -0.017 at 5d). Every consumer of this module renders the rating's realized performance
NEUTRALLY — IC always shown against its own ``noise_floor_ic``, never labeled a
recommendation, and the existing tearsheet/Holdings disclaimer stays as-is.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RATING_PERFORMANCE_KEY = "market_data/technicals/rating_performance.json"

# The producer contract this consumer understands (tests/contracts/rating_performance.schema.json).
# A producer bump to schema_version needs a matching consumer change, never a silent
# best-effort parse of an unpinned shape — an unrecognized version degrades to "absent".
SCHEMA_VERSION = 1

# Exactly the producer's label set (mirrors technical_rating.RATING_LABELS) — a label
# outside this set is dropped rather than surfaced verbatim.
RATING_LABELS: frozenset[str] = frozenset({"Strong Sell", "Sell", "Neutral", "Buy", "Strong Buy"})

# The producer's segment vocabulary. "live" = out-of-sample (only segment with a genuine
# forward-looking claim); "backfill" = simulated on today's universe (survivorship, NOT
# out-of-sample); "all" = both pooled. A segment may be entirely absent (e.g. "live" before
# any live dates exist) — never synthesized as zeros.
SEGMENTS: frozenset[str] = frozenset({"live", "backfill", "all"})


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=RATING_PERFORMANCE_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "no track record"
        logger.warning("data-spine read failed %s: %s", RATING_PERFORMANCE_KEY, e)
        return None


@dataclass
class RatingBucket:
    n: int | None = None
    mean_fwd: float | None = None
    hit_rate: float | None = None
    mean_excess: float | None = None


@dataclass
class RatingHorizonStats:
    # label ("Strong Sell" … "Strong Buy") -> bucket. Missing labels (n=0 dropped upstream,
    # or simply not yet published) are absent from this dict — never a fabricated zero row.
    buckets: dict[str, RatingBucket] = field(default_factory=dict)
    spread_strong_buy_minus_strong_sell: float | None = None
    ic_mean: float | None = None
    ic_n_dates: int | None = None
    # Always carried alongside ic_mean so every UI surface renders the IC NEXT TO its own
    # noise floor (2026-09-14 backfill grade: IC ~= -0.017 at 5d — no predictive edge).
    noise_floor_ic: float | None = None


@dataclass
class IcPoint:
    date: str
    horizon: int
    ic: float | None


@dataclass
class RatingPerformance:
    schema_version: int
    as_of_utc: str | None
    rating_version: str | None
    horizons: list[int] = field(default_factory=list)
    windows: list[int] = field(default_factory=list)
    # segment -> window (JSON string of an int) -> horizon (JSON string of an int) -> stats.
    segments: dict[str, dict[str, dict[str, RatingHorizonStats]]] = field(default_factory=dict)
    ic_series: list[IcPoint] = field(default_factory=list)

    def stats_for(self, *, segment: str, window: int, horizon: int) -> RatingHorizonStats | None:
        """The one (segment, window, horizon) cell, or None if that combination isn't
        published (e.g. ``live`` before any live dates exist, or an unrequested horizon).
        Never synthesize zeros for a missing cell — the caller renders "no data", not 0."""
        seg = self.segments.get(segment)
        if seg is None:
            return None
        win = seg.get(str(window))
        if win is None:
            return None
        return win.get(str(horizon))

    def bucket_for_label(
        self, label: str, *, segment: str, window: int, horizon: int
    ) -> RatingBucket | None:
        stats = self.stats_for(segment=segment, window=window, horizon=horizon)
        if stats is None:
            return None
        return stats.buckets.get(label)


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(d: dict, key: str) -> int | None:
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_bucket(d: dict) -> RatingBucket:
    return RatingBucket(
        n=_i(d, "n"), mean_fwd=_f(d, "mean_fwd"), hit_rate=_f(d, "hit_rate"), mean_excess=_f(d, "mean_excess")
    )


def _parse_horizon_stats(d: dict) -> RatingHorizonStats:
    raw_buckets = d.get("buckets")
    buckets: dict[str, RatingBucket] = {}
    if isinstance(raw_buckets, dict):
        for label, body in raw_buckets.items():
            if label in RATING_LABELS and isinstance(body, dict):
                buckets[label] = _parse_bucket(body)
    return RatingHorizonStats(
        buckets=buckets,
        spread_strong_buy_minus_strong_sell=_f(d, "spread_strong_buy_minus_strong_sell"),
        ic_mean=_f(d, "ic_mean"),
        ic_n_dates=_i(d, "ic_n_dates"),
        noise_floor_ic=_f(d, "noise_floor_ic"),
    )


def _parse_ic_series(raw: object) -> list[IcPoint]:
    points: list[IcPoint] = []
    if not isinstance(raw, list):
        return points
    for row in raw:
        if not isinstance(row, dict):
            continue
        raw_date, raw_horizon = row.get("date"), row.get("horizon")
        if not isinstance(raw_date, str) or raw_horizon is None:
            continue
        try:
            horizon = int(raw_horizon)
        except (TypeError, ValueError):
            continue
        points.append(IcPoint(date=raw_date, horizon=horizon, ic=_f(row, "ic")))
    return points


def _int_list(raw: object) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def load_rating_performance(*, reader=None) -> RatingPerformance | None:
    """The rating's realized track record, or None when the producer artifact is absent,
    unparseable, or on a ``schema_version`` this consumer doesn't understand. ``reader`` is
    the injectable test seam (a no-arg callable returning the raw artifact dict); defaults
    to the S3 read."""
    art = (reader or _default_reader)()
    if not isinstance(art, dict):
        return None
    schema_version = art.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        logger.warning(
            "rating_performance schema_version %r != pinned %d; omitting", schema_version, SCHEMA_VERSION
        )
        return None

    segments: dict[str, dict[str, dict[str, RatingHorizonStats]]] = {}
    raw_segments = art.get("segments")
    if isinstance(raw_segments, dict):
        for seg_key, windows in raw_segments.items():
            if seg_key not in SEGMENTS or not isinstance(windows, dict):
                continue
            by_window: dict[str, dict[str, RatingHorizonStats]] = {}
            for win_key, horizons in windows.items():
                if not isinstance(horizons, dict):
                    continue
                by_horizon: dict[str, RatingHorizonStats] = {
                    hz_key: _parse_horizon_stats(stats)
                    for hz_key, stats in horizons.items()
                    if isinstance(stats, dict)
                }
                if by_horizon:
                    by_window[win_key] = by_horizon
            if by_window:
                segments[seg_key] = by_window

    return RatingPerformance(
        schema_version=schema_version,
        as_of_utc=art.get("as_of_utc") if isinstance(art.get("as_of_utc"), str) else None,
        rating_version=art.get("rating_version") if isinstance(art.get("rating_version"), str) else None,
        horizons=_int_list(art.get("horizons")),
        windows=_int_list(art.get("windows")),
        segments=segments,
        ic_series=_parse_ic_series(art.get("ic_series")),
    )
