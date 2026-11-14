"""The remaining spine readers name the producer object they were read from (metron-ops-I373).

Same contract as ``test_snapshot_lineage.py`` (fundamentals, technicals), extended to the
analyst, sentiment, security-performance, valuation-medians, technical-rating,
rating-performance, intraday and indices readers. Synthetic artifacts and object versions only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from test_snapshot_lineage import _S3, BUCKET  # noqa: F401 - the `s3` fixture below uses _S3

from api.services import (
    analyst,
    indices,
    intraday,
    market_snapshot,
    security_performance,
    sentiment,
    spine_artifacts,
    technical_rating,
    technical_rating_performance,
    valuation_medians,
)
from portfolio_analytics.prices import InputSource

_NOW = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)
_SESSION = "2026-09-14"
_META = {"as_of": "2026-11-13", "source": "t"}

_ANALYST = {"schema_version": 1, **_META, "analyst": {"AAPL": {"mean_target": 250.0}}}
_SENTIMENT = {"schema_version": 1, **_META, "sentiment": {"AAPL": {"sentiment": 0.3}}}
_PERF = {"schema_version": 1, **_META, "performance": {"AAPL": {"ytd_pct": 0.1}}}
_MEDIANS = {"schema_version": 1, **_META, "by_sector": {"Tech": {"n": 70, "trailing_pe": 30.0}}, "by_country": {}}
_RATING_PERF = {"schema_version": 1, "as_of_utc": "2026-11-13T00:00:00Z", "rating_version": 1, "segments": {}}
_INTRADAY = {
    "schema_version": market_snapshot.INTRADAY_SCHEMA_VERSION,
    "as_of_utc": "2026-09-14T14:58:00Z",
    "source": "yfinance_delayed",
    "quotes": {"AAPL": {"last": 101.0, "session_date": _SESSION}},
    "indices": {"SPY": {"last": 101.0, "prev_close": 100.0, "open": 100.5, "session_date": _SESSION}},
}

Loaded = tuple[object, InputSource | None]
#: (artifact key, artifact, loader -> (snapshot, its input_source), schema-refused loader likewise)
CASES: list[tuple[str, dict, Callable[[], Loaded]]] = [
    (analyst.ANALYST_KEY, _ANALYST, lambda: (lambda s: (s, s.input_source))(analyst.load_analyst())),
    (sentiment.SENTIMENT_KEY, _SENTIMENT, lambda: (lambda s: (s, s.input_source))(sentiment.load_sentiment())),
    (
        security_performance.SECURITY_PERFORMANCE_KEY,
        _PERF,
        lambda: (lambda s: (s, s.input_source))(security_performance.load_security_performance()),
    ),
    (
        valuation_medians.VALUATION_MEDIANS_KEY,
        _MEDIANS,
        lambda: (lambda s: (s, s.input_source))(valuation_medians.load_valuation_medians()),
    ),
    (
        market_snapshot.INTRADAY_KEY,
        _INTRADAY,
        lambda: (lambda s: (s, s.input_source))(indices.load_indices(now=_NOW)),
    ),
]
IDS = ["analyst", "sentiment", "security_performance", "valuation_medians", "indices"]


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> _S3:
    stub = _S3()
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: stub)
    monkeypatch.delenv("MARKET_DATA_BUCKET", raising=False)
    spine_artifacts.clear()
    monkeypatch.setattr(market_snapshot, "_snapshot_cache", (None, None))
    monkeypatch.setattr(market_snapshot, "_snapshot_fetched_monotonic", float("-inf"))
    return stub


@pytest.mark.parametrize(("key", "art", "load"), CASES, ids=IDS)
def test_the_default_read_names_the_object_version_it_came_from(s3, key, art, load):
    s3.objects[key] = (art, "v-123", "abc")
    _snap, source = load()
    assert source == InputSource(artifact=f"s3://{BUCKET}/{key}", version_id="v-123", etag="abc")


@pytest.mark.parametrize(("key", "art", "load"), CASES, ids=IDS)
def test_an_unversioned_object_falls_back_to_its_etag(s3, key, art, load):
    s3.objects[key] = (art, "null", "abc")
    _snap, source = load()
    assert source is not None
    assert source.ref == f"s3://{BUCKET}/{key}?etag=abc"


@pytest.mark.parametrize(("key", "art", "load"), CASES, ids=IDS)
def test_a_refused_artifact_names_the_object_it_refused(s3, key, art, load):
    s3.objects[key] = ({**art, "schema_version": 999}, "v-bad", "def")
    snap, source = load()
    assert source is not None
    assert source.version_id == "v-bad"
    assert getattr(snap, "rejected_reason", None) is not None


@pytest.mark.parametrize(("key", "art", "load"), CASES, ids=IDS)
def test_a_failed_read_has_no_lineage(s3, key, art, load):
    _snap, source = load()  # nothing stored at the key
    assert source is None


@pytest.mark.parametrize(("key", "art", "load"), CASES[:4], ids=IDS[:4])
def test_a_cache_hit_keeps_the_lineage_of_the_bytes_it_serves(s3, key, art, load):
    s3.objects[key] = (art, "v-1", "e1")
    assert load()[1].version_id == "v-1"
    s3.objects[key] = (art, "v-2", "e2")
    assert load()[1].version_id == "v-1"  # within the TTL the cached bytes are still v-1
    assert s3.calls == 1
    spine_artifacts.clear()
    assert load()[1].version_id == "v-2"


def test_the_intraday_snapshot_cache_keeps_the_lineage_of_the_bytes_it_serves(s3):
    s3.objects[market_snapshot.INTRADAY_KEY] = (_INTRADAY, "v-1", "e1")
    assert indices.load_indices(now=_NOW).input_source.version_id == "v-1"
    s3.objects[market_snapshot.INTRADAY_KEY] = (_INTRADAY, "v-2", "e2")
    assert indices.load_indices(now=_NOW).input_source.version_id == "v-1"
    assert s3.calls == 1


def test_the_overlay_and_the_strip_share_one_read_and_one_lineage(s3):
    s3.objects[market_snapshot.INTRADAY_KEY] = (_INTRADAY, "v-7", "e7")
    quotes, _as_of, stale = intraday.load_quotes(now=_NOW)
    assert "AAPL" in quotes
    assert not stale
    assert indices.load_indices(now=_NOW).input_source.version_id == "v-7"
    assert s3.calls == 1


def test_the_shared_intraday_cache_names_what_it_serves_even_when_stale_or_refused(s3):
    s3.objects[market_snapshot.INTRADAY_KEY] = ({**_INTRADAY, "schema_version": 999}, "v-bad", "e4")
    art, source = market_snapshot.read_cached_artifact()
    assert art is not None
    assert source is not None
    assert source.version_id == "v-bad"
    assert intraday.load_quotes(now=_NOW)[0] == {}  # the overlay still refuses it whole


def test_technical_ratings_name_each_artifact_they_read(s3, monkeypatch):
    monkeypatch.setattr(technical_rating.entitlements, "current_pin", lambda: None)
    ratings = {
        "schema_version": technical_rating.TECHNICAL_RATINGS_SCHEMA_VERSION,
        "as_of_utc": "2026-09-14T14:58:00Z",
        "ratings": {"AAPL": {"score": 0.4, "label": "Buy"}},
    }
    tech = {"schema_version": 4, "as_of": "2026-09-11", "technicals": {}}
    s3.objects[technical_rating.TECHNICAL_RATINGS_KEY] = (ratings, "v-r", "er")
    s3.objects[technical_rating.TECHNICALS_KEY] = (tech, "v-t", "et")
    snap = technical_rating.load_technical_rating(now=_NOW)
    assert "AAPL" in snap.by_symbol
    assert {k: v.version_id for k, v in snap.input_sources.items()} == {
        technical_rating.TECHNICAL_RATINGS_KEY: "v-r",
        technical_rating.TECHNICALS_KEY: "v-t",
    }
    # An absent artifact contributes no lineage; a refused one still names its object.
    spine_artifacts.clear()
    del s3.objects[technical_rating.TECHNICAL_RATINGS_KEY]
    s3.objects[technical_rating.TECHNICALS_KEY] = ({**tech, "schema_version": 999}, "v-bad", "eb")
    snap = technical_rating.load_technical_rating(now=_NOW)
    assert set(snap.input_sources) == {technical_rating.TECHNICALS_KEY}
    assert snap.rejected


def test_rating_performance_names_the_object_it_was_read_from(s3):
    key = technical_rating_performance.RATING_PERFORMANCE_KEY
    s3.objects[key] = (_RATING_PERF, "v-p", "ep")
    perf = technical_rating_performance.load_rating_performance()
    assert perf is not None
    assert perf.input_source == InputSource(artifact=f"s3://{BUCKET}/{key}", version_id="v-p", etag="ep")


def test_injected_readers_cannot_say_which_object_they_read():
    assert analyst.load_analyst(reader=lambda: _ANALYST).input_source is None
    assert sentiment.load_sentiment(reader=lambda: _SENTIMENT).input_source is None
    assert security_performance.load_security_performance(reader=lambda: _PERF).input_source is None
    assert valuation_medians.load_valuation_medians(reader=lambda: _MEDIANS).input_source is None
    assert indices.load_indices(reader=lambda: _INTRADAY, now=_NOW).input_source is None
    perf = technical_rating_performance.load_rating_performance(reader=lambda: _RATING_PERF)
    assert perf is not None
    assert perf.input_source is None
