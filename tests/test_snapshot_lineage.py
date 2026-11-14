"""The fundamentals and technicals snapshots name the producer object they were read from
(metron-ops-I373).

``market_data/fundamentals/latest.json`` and ``market_data/technicals/latest.json`` are
overwritten every producer run, so a snapshot that kept only the declared
``schema_version`` / ``source`` could not say WHICH version of the artifact its metrics came
from. The default read now records an ``InputSource`` (``s3://`` location, S3 VersionId,
ETag) on the snapshot, in the same shape the price and FX readers use since #644.

Synthetic artifacts and object versions only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from api.services import fundamentals, spine_artifacts, technicals
from portfolio_analytics.prices import InputSource, s3_input_source

BUCKET = "alpha-engine-research"

_FUNDAMENTALS = {
    "schema_version": fundamentals.FUNDAMENTALS_SCHEMA_VERSION,
    "as_of": "2026-11-13",
    "source": "yfinance",
    "fundamentals": {"AAPL": {"trailingPE": 30.0, "marketCap": 3.0e12}},
}
_TECHNICALS = {
    "schema_version": technicals.TECHNICALS_SCHEMA_VERSION,
    "as_of": "2026-11-13",
    "source": "computed",
    "technicals": {"AAPL": {"rsi_14": 55.0, "ma_50": 190.0}},
}

Loader = Callable[[], fundamentals.FundamentalsSnapshot | technicals.TechnicalsSnapshot]
READERS: list[tuple[str, dict, Loader]] = [
    (fundamentals.FUNDAMENTALS_KEY, _FUNDAMENTALS, lambda: fundamentals.load_fundamentals()),
    (technicals.TECHNICALS_KEY, _TECHNICALS, lambda: technicals.load_technicals()),
]
IDS = ["fundamentals", "technicals"]


class _S3:
    """A stub S3 serving ``key -> (json, VersionId, ETag)``; the served object can be
    replaced between reads, as a producer run replaces it."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[object, str | None, str | None]] = {}
        self.calls = 0

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803 - boto3 kwarg names
        assert Bucket == BUCKET
        self.calls += 1
        if Key not in self.objects:
            raise Exception(f"NoSuchKey: {Key}")
        art, version_id, etag = self.objects[Key]
        body = MagicMock()
        body.read.return_value = json.dumps(art).encode()
        out: dict = {"Body": body}
        if version_id is not None:
            out["VersionId"] = version_id
        if etag is not None:
            out["ETag"] = f'"{etag}"'
        return out


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> _S3:
    stub = _S3()
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: stub)
    monkeypatch.delenv("MARKET_DATA_BUCKET", raising=False)
    return stub


@pytest.mark.parametrize(("key", "art", "load"), READERS, ids=IDS)
def test_the_default_read_names_the_object_version_every_metric_came_from(s3, key, art, load):
    s3.objects[key] = (art, "v-123", "abc")
    snap = load()
    assert "AAPL" in snap.by_symbol
    assert snap.input_source == InputSource(artifact=f"s3://{BUCKET}/{key}", version_id="v-123", etag="abc")
    assert snap.input_source.ref == f"s3://{BUCKET}/{key}?versionId=v-123"


@pytest.mark.parametrize(("key", "art", "load"), READERS, ids=IDS)
def test_an_unversioned_object_falls_back_to_its_content_digest(s3, key, art, load):
    # S3 reports VersionId "null" for an object written before versioning was enabled.
    s3.objects[key] = (art, "null", "abc")
    snap = load()
    assert snap.input_source is not None
    assert snap.input_source.version_id is None
    assert snap.input_source.ref == f"s3://{BUCKET}/{key}?etag=abc"


@pytest.mark.parametrize(("key", "art", "load"), READERS, ids=IDS)
def test_a_refused_artifact_names_the_object_it_refused(s3, key, art, load):
    s3.objects[key] = ({**art, "schema_version": 999}, "v-bad", "def")
    snap = load()
    assert snap.by_symbol == {}
    assert snap.rejected_reason is not None
    assert snap.input_source is not None
    assert snap.input_source.version_id == "v-bad"


@pytest.mark.parametrize(("key", "art", "load"), READERS, ids=IDS)
def test_a_failed_read_has_no_lineage_and_no_metrics(s3, key, art, load):
    snap = load()  # nothing stored at the key
    assert snap.by_symbol == {}
    assert snap.rejected_reason is None
    assert snap.input_source is None


@pytest.mark.parametrize(("key", "art", "load"), READERS, ids=IDS)
def test_a_cache_hit_keeps_the_lineage_of_the_bytes_it_serves(s3, key, art, load):
    s3.objects[key] = (art, "v-1", "e1")
    assert load().input_source.version_id == "v-1"
    # The producer overwrites the key. Within the TTL the cached bytes are still v-1, so
    # the lineage must say v-1, not whatever the key holds now.
    s3.objects[key] = (art, "v-2", "e2")
    cached = load()
    assert s3.calls == 1
    assert cached.input_source.version_id == "v-1"
    spine_artifacts.clear()
    assert load().input_source.version_id == "v-2"


def test_an_injected_reader_cannot_say_which_object_it_read():
    assert fundamentals.load_fundamentals(reader=lambda: _FUNDAMENTALS).input_source is None
    assert technicals.load_technicals(reader=lambda: _TECHNICALS).input_source is None


def test_read_json_still_returns_the_bare_artifact(s3):
    s3.objects[fundamentals.FUNDAMENTALS_KEY] = (_FUNDAMENTALS, "v-1", "e1")
    assert spine_artifacts.read_json(fundamentals.FUNDAMENTALS_KEY) == _FUNDAMENTALS


def test_a_json_null_body_reads_as_absent_and_is_not_cached(s3):
    s3.objects[fundamentals.FUNDAMENTALS_KEY] = (None, "v-1", "e1")
    assert spine_artifacts.read_artifact(fundamentals.FUNDAMENTALS_KEY) == (None, None)
    spine_artifacts.read_artifact(fundamentals.FUNDAMENTALS_KEY)
    assert s3.calls == 2


def test_snapshots_and_closes_name_their_object_the_same_way():
    response = {"VersionId": "v-9", "ETag": '"d1"'}
    src = s3_input_source(BUCKET, "market_data/close_history/AAPL.json", response, {"adjustment_basis": "dividend_adjusted"})
    assert src == InputSource(
        artifact=f"s3://{BUCKET}/market_data/close_history/AAPL.json",
        version_id="v-9",
        etag="d1",
        adjustment_basis="dividend_adjusted",
    )
    # A snapshot artifact declares no basis; a non-object payload has none to declare.
    assert s3_input_source(BUCKET, "k", {}, [1, 2]) == InputSource(artifact=f"s3://{BUCKET}/k")
