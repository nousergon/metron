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

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from api.services import spine_artifacts
from api.services.spine_artifacts import ArtifactReader, JsonObject
from portfolio_analytics import admission
from portfolio_analytics.admission import admit_schema, finite_field
from portfolio_analytics.prices.source import InputSource

logger = logging.getLogger(__name__)

SENTIMENT_KEY = "market_data/sentiment/latest.json"
# Pinned producer version (tests/contracts/metron_sentiment.schema.json). Any other version is
# refused whole at runtime (metron-ops-I373).
SENTIMENT_SCHEMA_VERSION = 1


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
    # Set when the artifact was refused whole (off-contract schema_version): it then reads
    # as absent, but the reason is explicit. `invalid_fields` names, per symbol, the fields
    # that were present but not a finite number and therefore read as unavailable.
    rejected_reason: str | None = None
    invalid_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The stored producer object this snapshot was read from: its `s3://` location and
    # the VersionId / ETag S3 reported on the read (metron-ops-I373). Set for a refused
    # artifact too. `None` when the read failed or the reader was injected.
    input_source: InputSource | None = None
    # Set when the content's own `as_of` is too old (or absent), whatever the object's write
    # time says; the numbers are still served, so this is a flag, not a refusal (I373).
    stale_reason: str | None = None
    # Set when a PRESENT artifact's symbol map is empty or far below the product's minimum
    # universe, so "no coverage" is not mistaken for a healthy producer (I373).
    universe_reason: str | None = None


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_read() -> tuple[JsonObject | None, InputSource | None]:
    # Fail-soft ((None, None) on any read error); shared short-TTL cache, and the object
    # version the bytes came from — see spine_artifacts.
    return spine_artifacts.read_artifact(SENTIMENT_KEY, bucket=_bucket())


def _i(d: dict, key: str) -> int | None:
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError, OverflowError):  # int(inf) raises OverflowError
        return None


def _d(d: dict, key: str) -> date | None:
    v = d.get(key)
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _parse(yf_symbol: str, d: dict, invalid: list[str]) -> TickerSentiment:
    def _f(d: dict, key: str) -> float | None:
        return finite_field(d, key, invalid)

    return TickerSentiment(
        yf_symbol=yf_symbol,
        sentiment=_f(d, "sentiment"),
        sentiment_mean=_f(d, "sentiment_mean"),
        n_articles=_i(d, "n_articles"),
        event_count=_i(d, "event_count"),
        event_severity_max=_f(d, "event_severity_max"),
        as_of=_d(d, "as_of"),
    )


def load_sentiment(*, reader: ArtifactReader | None = None, now: datetime | None = None) -> SentimentSnapshot:
    """The latest news-sentiment snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read. A
    missing artifact yields an empty snapshot (fail-soft)."""
    if reader is not None:
        raw, input_source = reader(), None
    else:
        raw, input_source = _default_read()
    rejected = admit_schema(raw, expected=SENTIMENT_SCHEMA_VERSION, artifact=SENTIMENT_KEY)
    if rejected is not None:
        return SentimentSnapshot(as_of=None, by_symbol={}, rejected_reason=rejected, input_source=input_source)
    art = raw or {}
    by_symbol: dict[str, TickerSentiment] = {}
    invalid_fields: dict[str, tuple[str, ...]] = {}
    for sym, body in (art.get("sentiment") or {}).items():
        if isinstance(body, dict):
            invalid: list[str] = []
            by_symbol[sym] = _parse(sym, body, invalid)
            if invalid:
                invalid_fields[sym] = tuple(invalid)
    if invalid_fields:
        logger.warning(
            "%s: %d symbol(s) carried non-finite or non-numeric values, read as unavailable: %s",
            SENTIMENT_KEY, len(invalid_fields), sorted(invalid_fields),
        )
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    stale_reason = (
        admission.content_staleness(
            as_of,
            now=now or datetime.now(UTC),
            written_at=input_source.last_modified if input_source else None,
            artifact=SENTIMENT_KEY,
        )
        if raw is not None
        else None
    )
    if stale_reason:
        logger.warning("%s", stale_reason)
    universe_reason = (
        admission.universe_shortfall(len(by_symbol), artifact=SENTIMENT_KEY, minimum=1) if raw is not None else None
    )
    return SentimentSnapshot(
        as_of=as_of,
        by_symbol=by_symbol,
        invalid_fields=invalid_fields,
        input_source=input_source,
        stale_reason=stale_reason,
        universe_reason=universe_reason,
    )
