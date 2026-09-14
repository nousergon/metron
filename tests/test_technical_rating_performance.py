"""Technical rating track record consumer (metron-ops#298, Brian ruling 2026-09-14) — the
reader against the producer artifact (``market_data/technicals/rating_performance.json``
v1), pinned schema, and the fail-soft/absent-artifact path. Pure unit tests (injected
readers — no S3, no network); the artifact does not exist in S3 yet (concurrent sibling
nousergon-data producer issue), which is exactly the "absent artifact" path these tests pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from api.services import technical_rating_performance as rp

SCHEMA_PATH = Path(__file__).parent / "contracts" / "rating_performance.schema.json"


def _art() -> dict:
    return {
        "schema_version": 1,
        "as_of_utc": "2026-09-14T05:00:00Z",
        "rating_version": "v1",
        "horizons": [1, 5, 20],
        "windows": [20, 60, 250],
        "segments": {
            "all": {
                "60": {
                    "5": {
                        "buckets": {
                            "Strong Sell": {"n": 40, "mean_fwd": -0.001, "hit_rate": 0.48, "mean_excess": -0.002},
                            "Sell": {"n": 120, "mean_fwd": 0.0005, "hit_rate": 0.50, "mean_excess": -0.0005},
                            "Neutral": {"n": 900, "mean_fwd": 0.001, "hit_rate": 0.51, "mean_excess": 0.0001},
                            "Buy": {"n": 130, "mean_fwd": 0.0015, "hit_rate": 0.52, "mean_excess": 0.0003},
                            "Strong Buy": {"n": 35, "mean_fwd": 0.0009, "hit_rate": 0.49, "mean_excess": -0.0003},
                        },
                        "spread_strong_buy_minus_strong_sell": 0.0019,
                        "ic_mean": -0.017,
                        "ic_n_dates": 180,
                        "noise_floor_ic": 0.02,
                    },
                    "1": {
                        "buckets": {},
                        "spread_strong_buy_minus_strong_sell": None,
                        "ic_mean": None,
                        "ic_n_dates": 0,
                        "noise_floor_ic": 0.02,
                    },
                },
            },
            "backfill": {
                "60": {
                    "5": {
                        "buckets": {
                            "Strong Buy": {"n": 20, "mean_fwd": 0.0011, "hit_rate": 0.50, "mean_excess": 0.0001},
                        },
                        "spread_strong_buy_minus_strong_sell": 0.001,
                        "ic_mean": -0.02,
                        "ic_n_dates": 120,
                        "noise_floor_ic": 0.02,
                    },
                },
            },
            # "live" deliberately absent — before any live dates exist (metron-ops#298 spec).
        },
        "ic_series": [
            {"date": "2026-09-10", "horizon": 5, "ic": -0.01},
            {"date": "2026-09-11", "horizon": 5, "ic": -0.03},
            {"date": "2026-09-12", "horizon": 5, "ic": None},
        ],
    }


# ── 0. the pinned producer schema is itself valid, and our fixture conforms ────────────


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def test_pinned_schema_is_valid_and_v1(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"] == 1


def test_fixture_conforms_to_pinned_schema(schema):
    jsonschema.validate(instance=_art(), schema=schema)


# ── 1. happy path ────────────────────────────────────────────────────────────────────


def test_load_parses_full_artifact():
    parsed = rp.load_rating_performance(reader=_art)
    assert parsed is not None
    assert parsed.schema_version == 1
    assert parsed.rating_version == "v1"
    assert parsed.horizons == [1, 5, 20]
    assert parsed.windows == [20, 60, 250]
    assert len(parsed.ic_series) == 3
    assert parsed.ic_series[1].ic == pytest.approx(-0.03)
    assert parsed.ic_series[2].ic is None


def test_stats_for_returns_the_cell():
    parsed = rp.load_rating_performance(reader=_art)
    stats = parsed.stats_for(segment="all", window=60, horizon=5)
    assert stats is not None
    assert stats.ic_mean == pytest.approx(-0.017)
    assert stats.noise_floor_ic == pytest.approx(0.02)
    assert stats.spread_strong_buy_minus_strong_sell == pytest.approx(0.0019)
    assert set(stats.buckets) == {"Strong Sell", "Sell", "Neutral", "Buy", "Strong Buy"}


def test_bucket_for_label():
    parsed = rp.load_rating_performance(reader=_art)
    b = parsed.bucket_for_label("Buy", segment="all", window=60, horizon=5)
    assert b is not None
    assert b.n == 130 and b.mean_excess == pytest.approx(0.0003) and b.hit_rate == pytest.approx(0.52)


# ── 2. missing-segment / missing-cell honesty (never zeros) ────────────────────────────


def test_missing_segment_is_none_never_zeros():
    parsed = rp.load_rating_performance(reader=_art)
    assert parsed.stats_for(segment="live", window=60, horizon=5) is None
    assert parsed.bucket_for_label("Buy", segment="live", window=60, horizon=5) is None


def test_missing_window_or_horizon_is_none():
    parsed = rp.load_rating_performance(reader=_art)
    assert parsed.stats_for(segment="all", window=20, horizon=5) is None
    assert parsed.stats_for(segment="all", window=60, horizon=20) is None


def test_empty_buckets_cell_still_parses_null_stats():
    parsed = rp.load_rating_performance(reader=_art)
    stats = parsed.stats_for(segment="all", window=60, horizon=1)
    assert stats is not None
    assert stats.buckets == {}
    assert stats.ic_mean is None
    assert stats.ic_n_dates == 0
    assert stats.noise_floor_ic == pytest.approx(0.02)


def test_backfill_segment_present_independently():
    parsed = rp.load_rating_performance(reader=_art)
    stats = parsed.stats_for(segment="backfill", window=60, horizon=5)
    assert stats is not None
    assert stats.ic_mean == pytest.approx(-0.02)


# ── 3. absent / malformed artifact -> the WHOLE thing is None, never partial ───────────


def test_reader_returns_none_artifact_absent():
    assert rp.load_rating_performance(reader=lambda: None) is None


def test_reader_returns_non_dict_is_absent():
    assert rp.load_rating_performance(reader=lambda: "not json") is None
    assert rp.load_rating_performance(reader=lambda: []) is None


def test_wrong_schema_version_is_treated_as_absent():
    art = {**_art(), "schema_version": 2}
    assert rp.load_rating_performance(reader=lambda: art) is None


def test_missing_schema_version_is_treated_as_absent():
    art = _art()
    del art["schema_version"]
    assert rp.load_rating_performance(reader=lambda: art) is None


def test_unknown_segment_key_is_dropped_not_fabricated():
    art = _art()
    art["segments"]["unknown_segment"] = art["segments"]["all"]
    parsed = rp.load_rating_performance(reader=lambda: art)
    assert "unknown_segment" not in parsed.segments


def test_unknown_label_is_dropped_never_surfaced():
    art = _art()
    art["segments"]["all"]["60"]["5"]["buckets"]["Bullish!"] = {"n": 1, "mean_fwd": 0.1, "hit_rate": 1.0, "mean_excess": 0.1}
    parsed = rp.load_rating_performance(reader=lambda: art)
    stats = parsed.stats_for(segment="all", window=60, horizon=5)
    assert "Bullish!" not in stats.buckets


def test_non_dict_horizons_and_non_numeric_fields_are_skipped():
    art = _art()
    # A window whose horizons value isn't a dict is dropped entirely.
    art["segments"]["all"]["250"] = "not a dict"
    # Non-numeric numeric-typed fields degrade to None rather than raising.
    art["segments"]["all"]["60"]["5"]["buckets"]["Neutral"]["n"] = "lots"
    art["segments"]["all"]["60"]["5"]["buckets"]["Neutral"]["mean_fwd"] = "n/a"
    parsed = rp.load_rating_performance(reader=lambda: art)
    assert "250" not in parsed.segments["all"]
    neutral = parsed.bucket_for_label("Neutral", segment="all", window=60, horizon=5)
    assert neutral.n is None and neutral.mean_fwd is None


def test_non_list_horizons_windows_and_ic_series_degrade_to_empty():
    art = _art()
    art["horizons"] = "not a list"
    art["windows"] = None
    art["ic_series"] = "not a list either"
    parsed = rp.load_rating_performance(reader=lambda: art)
    assert parsed.horizons == [] and parsed.windows == [] and parsed.ic_series == []


def test_malformed_ic_series_rows_are_skipped():
    art = _art()
    art["ic_series"] = [
        {"date": "2026-09-10", "horizon": 5, "ic": -0.01},
        {"date": "2026-09-11"},  # missing horizon
        "garbage",
        {"horizon": 5, "ic": 0.1},  # missing date
        {"date": "2026-09-12", "horizon": "five", "ic": 0.1},  # non-numeric horizon
    ]
    parsed = rp.load_rating_performance(reader=lambda: art)
    assert len(parsed.ic_series) == 1


def test_non_numeric_entries_in_horizons_windows_are_skipped():
    art = _art()
    art["horizons"] = [1, "bogus", 5]
    art["windows"] = [20, None, 60]
    parsed = rp.load_rating_performance(reader=lambda: art)
    assert parsed.horizons == [1, 5]
    assert parsed.windows == [20, 60]
