"""Technical rating (Strong Sell … Strong Buy, signed score in [-1, +1]) — the composite MA
+ oscillator vote from the alpha-engine-data **data spine** (metron-ops#294).

Two producer artifacts, read in freshness order — per SYMBOL, not per artifact, so a
partially-covered intraday snapshot still lets the other held symbols fall back cleanly:

- ``market_data/intraday/technical_ratings.json`` (schema v1) — computed off the live
  intraday snapshot; basis ``"intraday"``. Used for a symbol only while the artifact's own
  ``as_of_utc`` is within ``intraday.STALE_AFTER_SECONDS`` of "now" — the same freshness
  window the intraday price overlay uses (metron-ops#79) — ~15-min delayed during market
  hours, stale (unused) once the feed stops publishing (after close / weekend / outage).
- ``market_data/technicals/latest.json`` (schema v3) — each symbol entry carries an
  embedded ``rating`` object (the same MA/oscillator vote, computed EOD); basis ``"eod"``.
  Used as the fallback: the intraday artifact absent/stale, or a symbol simply has no
  intraday rating of its own. Schema v2 technicals (no embedded ``rating``) is tolerated —
  the EOD fallback is then empty for every symbol, never an error.

Mirrors `technicals.py` (module-level KEY, fail-soft `_default_reader`, injectable
`reader`, dataclass + `load_*`). Metron is a pure S3 consumer: a missing artifact / absent
symbol → omitted fields, never fabricated. The artifact does not exist yet in S3 at the
time this consumer ships (concurrent sibling producer PR) — every read degrades to "no
rating" exactly like any other absent data-spine artifact.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from api.services import intraday as intraday_service

logger = logging.getLogger(__name__)

TECHNICAL_RATINGS_KEY = "market_data/intraday/technical_ratings.json"
# Shared with technicals.py — this module reads the SAME artifact for its embedded EOD
# ``rating`` object, independently of (and without depending on) technicals.py's own
# TickerTechnicals parse, which doesn't carry the rating fields.
TECHNICALS_KEY = "market_data/technicals/latest.json"

# Exactly the producer's label set (metron-ops#294 spec) — a label outside this set is
# treated as absent rather than surfaced verbatim, so the UI never renders an unknown word.
RATING_LABELS: frozenset[str] = frozenset({"Strong Sell", "Sell", "Neutral", "Buy", "Strong Buy"})

BASIS_INTRADAY = "intraday"
BASIS_EOD = "eod"


@dataclass
class TickerRating:
    yf_symbol: str
    score: float | None            # signed [-1, +1]
    label: str | None              # one of RATING_LABELS
    ma_score: float | None
    osc_score: float | None
    n_buy: int | None
    n_neutral: int | None
    n_sell: int | None
    n_votes: int | None
    basis: str                     # "intraday" | "eod"
    as_of: str | None = None       # ISO8601 UTC datetime (intraday) or ISO date (eod)


@dataclass
class RatingSnapshot:
    by_symbol: dict[str, TickerRating]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_intraday_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=TECHNICAL_RATINGS_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to the EOD fallback
        logger.warning("data-spine read failed %s: %s", TECHNICAL_RATINGS_KEY, e)
        return None


def _default_technicals_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=TECHNICALS_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "no rating"
        logger.warning("data-spine read failed %s: %s", TECHNICALS_KEY, e)
        return None


def _is_stale(as_of_utc: str | None, now: datetime) -> bool:
    """Mirrors ``intraday._is_stale`` (module-local by repo convention — see
    crypto.py/indices.py/intraday.py) against the SAME ``STALE_AFTER_SECONDS`` window, so an
    intraday rating goes stale at exactly the moment the intraday price overlay does."""
    if not as_of_utc:
        return True
    try:
        beat = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - beat).total_seconds() > intraday_service.STALE_AFTER_SECONDS


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


def _label(d: dict) -> str | None:
    label = d.get("label")
    return label if isinstance(label, str) and label in RATING_LABELS else None


def _parse(yf_symbol: str, d: dict, *, basis: str, as_of: str | None) -> TickerRating:
    return TickerRating(
        yf_symbol=yf_symbol,
        score=_f(d, "score"),
        label=_label(d),
        ma_score=_f(d, "ma_score"),
        osc_score=_f(d, "osc_score"),
        n_buy=_i(d, "n_buy"),
        n_neutral=_i(d, "n_neutral"),
        n_sell=_i(d, "n_sell"),
        n_votes=_i(d, "n_votes"),
        basis=basis,
        as_of=as_of,
    )


def load_technical_rating(
    *, intraday_reader=None, technicals_reader=None, now: datetime | None = None
) -> RatingSnapshot:
    """The latest per-symbol technical rating, keyed by yf_symbol — intraday where fresh,
    EOD fallback otherwise, decided PER SYMBOL (metron-ops#294). ``intraday_reader`` /
    ``technicals_reader`` (no-arg callables returning the raw artifact dict) and ``now`` are
    injectable for tests; default to the S3 reads / wall clock."""
    now = now or datetime.now(UTC)
    by_symbol: dict[str, TickerRating] = {}

    intraday_art = (intraday_reader or _default_intraday_reader)() or {}
    intraday_as_of = intraday_art.get("as_of_utc")
    if intraday_art and not _is_stale(intraday_as_of, now):
        ratings = intraday_art.get("ratings")
        if isinstance(ratings, dict):
            for sym, body in ratings.items():
                if isinstance(body, dict):
                    by_symbol[sym] = _parse(sym, body, basis=BASIS_INTRADAY, as_of=intraday_as_of)

    # EOD fallback (schema v3 embedded ``rating``) — fills any symbol not already covered by
    # a fresh intraday rating, including every symbol when the intraday artifact is
    # absent/stale/missing its own ``ratings`` map. Schema v2 (no ``rating`` key per symbol)
    # degrades to simply contributing nothing here — never an error.
    tech_art = (technicals_reader or _default_technicals_reader)() or {}
    tech_as_of = tech_art.get("as_of")
    for sym, body in (tech_art.get("technicals") or {}).items():
        if sym in by_symbol or not isinstance(body, dict):
            continue
        rating = body.get("rating")
        if isinstance(rating, dict):
            by_symbol[sym] = _parse(sym, rating, basis=BASIS_EOD, as_of=str(tech_as_of) if tech_as_of else None)

    return RatingSnapshot(by_symbol=by_symbol)
