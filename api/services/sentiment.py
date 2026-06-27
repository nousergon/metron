"""News sentiment from the alpha-engine-data **data spine** (metron-ops#105, Phase 1).

Reads `market_data/sentiment/latest.json` (produced by alpha-engine-data's metron_market_data
`collect_sentiment` — a JSON projection of the held-universe latest slice of the upstream
`news_aggregates_daily` Loughran-McDonald sentiment + event rollup; FREE sources → feed-gated),
keyed by yf_symbol. Metron is a pure S3 consumer: a missing artifact / absent symbol → omitted,
never fabricated. The source is injectable for tests.

Mirrors `fundamentals.py` / `technicals.py` exactly (module-level KEY, fail-soft
`_default_reader`, injectable `reader`, dataclass + `load_*`).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

SENTIMENT_KEY = "market_data/sentiment/latest.json"


@dataclass
class TickerSentiment:
    yf_symbol: str
    sentiment: float | None          # trust-weighted LM composite ∈ [-1, +1] (headline metric)
    sentiment_mean: float | None     # raw (unweighted) LM mean — kept for audit
    n_articles: int | None           # # of articles behind the score
    event_count: int | None          # # of detected news events in the window
    event_severity_max: float | None  # max event severity in the window
    # The aggregate date of the row this slice came from — lets the consumer show sentiment
    # staleness honestly (per-ticker, not one global as_of).
    as_of: date | None


@dataclass
class SentimentSnapshot:
    as_of: date | None               # the artifact's run_date (the slice's freshness anchor)
    by_symbol: dict[str, TickerSentiment]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=SENTIMENT_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "sentiment unavailable"
        logger.warning("data-spine read failed %s: %s", SENTIMENT_KEY, e)
        return None


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


def _d(d: dict, key: str) -> date | None:
    v = d.get(key)
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _parse(yf_symbol: str, d: dict) -> TickerSentiment:
    return TickerSentiment(
        yf_symbol=yf_symbol,
        sentiment=_f(d, "sentiment"),
        sentiment_mean=_f(d, "sentiment_mean"),
        n_articles=_i(d, "n_articles"),
        event_count=_i(d, "event_count"),
        event_severity_max=_f(d, "event_severity_max"),
        as_of=_d(d, "as_of"),
    )


def load_sentiment(*, reader=None) -> SentimentSnapshot:
    """The latest news-sentiment snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read. A
    missing artifact yields an empty snapshot (fail-soft)."""
    art = (reader or _default_reader)() or {}
    by_symbol: dict[str, TickerSentiment] = {}
    for sym, body in (art.get("sentiment") or {}).items():
        if isinstance(body, dict):
            by_symbol[sym] = _parse(sym, body)
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return SentimentSnapshot(as_of=as_of, by_symbol=by_symbol)
