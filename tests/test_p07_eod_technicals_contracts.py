"""Consumer contract tests for three data-spine artifacts that had no pinned producer schema.

Data-collector plan P-07 (alpha-engine-config-I10774, I11282). The gate's
`schema_contract` clause wants each consumer to pin a copy of the producer schema,
so a producer change shows up as a failure in this repo. These three artifacts were
read here with nothing pinned:

* ``market_data/technicals/latest.json``: nousergon-data unit D25, read by
  ``api.services.technicals.load_technicals``. Pinned copy:
  ``tests/contracts/technicals.schema.json``.
* ``market_data/technicals/rating_performance.json``: unit D27, read by
  ``api.services.technical_rating_performance.load_rating_performance``. Pinned copy:
  ``tests/contracts/rating_performance.producer.schema.json``. The older
  ``rating_performance.schema.json`` beside it was written before the producer
  existed. It is the reader's own tolerance schema, not a copy of the producer's,
  and ``test_technical_rating_performance.py`` still uses it.
* ``market_data/index_contributions/{index}/{date}.json``: unit D48, read by
  ``portfolio_analytics.index_contributions.spine_source.spine_index_contributions``
  through ``fetch_index_contributions``. Pinned copy:
  ``tests/contracts/index_contributions.schema.json``.

Each pinned file is a byte copy of ``nousergon-data/contracts/<same>.schema.json`` at
the time of pinning (the producer name is kept, except where it would have collided).
The gate compares each copy's validation shape with the producer's on every run, so a
stale copy reads UNMET there. To re-pin, copy the producer file again and update the
fixtures until this file passes.

The technicals and rating-performance fixtures are trimmed from the real
2026-10-01 EOD artifacts (``s3://alpha-engine-research/market_data/technicals/``),
which validate against the producer schemas as published. The index-contributions
artifact has never been written (D48 is not scheduled yet, alpha-engine-config-I11306),
so its fixture is built from the producer schema and its stated identity
``weight_prior_close * return_pct == contribution_pp``.
"""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import jsonschema
import pytest

from api.services import technical_rating_performance as rating_performance_svc
from api.services import technicals as technicals_svc
from portfolio_analytics.index_contributions import spine_source as index_contributions_spine
from portfolio_analytics.index_contributions.source import fetch_index_contributions

CONTRACTS_DIR = Path(__file__).parent / "contracts"

PINNED = {
    "technicals": "technicals.schema.json",
    "rating_performance": "rating_performance.producer.schema.json",
    "index_contributions": "index_contributions.schema.json",
}


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / PINNED[name]).read_text())


def _validate(payload: dict, name: str) -> None:
    jsonschema.validate(instance=payload, schema=_schema(name))


def _s3_serving(objects: dict[str, dict]):
    """A minimal S3 stand-in: ``get_object`` serves ``objects`` and raises on any other key."""

    def _get(Bucket, Key):  # noqa: N803 - boto3's keyword names
        if Key not in objects:
            raise KeyError(f"NoSuchKey: {Key}")
        body = json.dumps(objects[Key]).encode()
        return {"Body": type("B", (), {"read": lambda self: body})()}

    return type("S3", (), {"get_object": staticmethod(_get)})()


@pytest.mark.parametrize("name", sorted(PINNED))
def test_pinned_schema_is_valid(name):
    jsonschema.Draft202012Validator.check_schema(_schema(name))


# ── D25: market_data/technicals/latest.json ──────────────────────────────────────────

# Two symbols from the real 2026-10-01 artifact (929 symbols, schema_version 4).
_TECHNICALS = {
    "as_of": "2026-10-01",
    "schema_version": 4,
    "source": "computed",
    "technicals": {
        "A": {
            "high_52w": 175.21, "low_52w": 109.7779, "ma_200": 132.407, "ma_50": 151.9708,
            "macd_hist": 1.0629, "mom_20d": 0.101, "mom_60d": 0.2914,
            "pct_from_52wk_high": -0.0487, "pct_in_52w_range": 0.8696,
            "pct_to_ma_200": 0.2588, "pct_to_ma_50": 0.0968,
            "rating": {
                "label": "Buy", "ma_score": 0.5385, "n_buy": 11, "n_neutral": 1, "n_sell": 4,
                "n_votes": 16, "osc_score": 0.0, "rating_version": 2, "score": 0.2692,
            },
            "rsi_14": 59.95,
        },
        "AA": {
            "high_52w": 83.6277, "low_52w": 33.5253, "ma_200": 57.8439, "ma_50": 47.6248,
            "macd_hist": -0.3814, "mom_20d": -0.1761, "mom_60d": -0.1313,
            "pct_from_52wk_high": -0.4971, "pct_in_52w_range": 0.1703,
            "pct_to_ma_200": -0.2729, "pct_to_ma_50": -0.1168,
            "rating": {
                "label": "Strong Sell", "ma_score": -0.8462, "n_buy": 1, "n_neutral": 1,
                "n_sell": 14, "n_votes": 16, "osc_score": -0.6667, "rating_version": 2,
                "score": -0.7564,
            },
            "rsi_14": 30.78,
        },
    },
}


def test_technicals_fixture_validates_and_reader_extracts_fields():
    _validate(_TECHNICALS, "technicals")
    snap = technicals_svc.load_technicals(reader=lambda: copy.deepcopy(_TECHNICALS))
    assert snap.as_of == date(2026, 10, 1)
    assert set(snap.by_symbol) == {"A", "AA"}
    a = snap.by_symbol["A"]
    assert a.rsi_14 == pytest.approx(59.95)
    assert a.pct_to_ma_50 == pytest.approx(0.0968)
    assert a.pct_in_52w_range == pytest.approx(0.8696)
    assert snap.by_symbol["AA"].mom_60d == pytest.approx(-0.1313)


def test_technicals_stringified_number_fails_schema():
    # The reader would coerce "59.95" through float() without complaint, so only the
    # pinned schema notices a producer that starts writing numbers as strings.
    art = copy.deepcopy(_TECHNICALS)
    art["technicals"]["A"]["rsi_14"] = "59.95"
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "technicals")


def test_technicals_unknown_schema_version_fails_schema():
    art = {**copy.deepcopy(_TECHNICALS), "schema_version": 5}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "technicals")


# ── D27: market_data/technicals/rating_performance.json ──────────────────────────────

_BUCKETS_ALL_60_5 = {
    "Buy": {"hit_rate": 0.46416, "mean_excess": -0.00199, "mean_fwd": -0.002487, "n": 16755},
    "Neutral": {"hit_rate": None, "mean_excess": 0.000697, "mean_fwd": -0.001088, "n": 5835},
    "Sell": {"hit_rate": 0.550972, "mean_excess": 0.002122, "mean_fwd": -0.001046, "n": 16097},
    "Strong Buy": {"hit_rate": 0.448341, "mean_excess": -0.004299, "mean_fwd": -0.00402, "n": 8953},
    "Strong Sell": {"hit_rate": 0.550486, "mean_excess": 0.003712, "mean_fwd": 0.000514, "n": 9052},
}

_BUCKETS_LIVE_60_5 = {
    "Buy": {"hit_rate": 0.393408, "mean_excess": 0.002322, "mean_fwd": -0.006398, "n": 1426},
    "Neutral": {"hit_rate": None, "mean_excess": 0.005087, "mean_fwd": -0.003018, "n": 703},
    "Sell": {"hit_rate": 0.658472, "mean_excess": -0.00048, "mean_fwd": -0.008572, "n": 3376},
    "Strong Buy": {"hit_rate": 0.464544, "mean_excess": 0.008236, "mean_fwd": -0.001138, "n": 691},
    "Strong Sell": {"hit_rate": 0.696721, "mean_excess": -0.004727, "mean_fwd": -0.012413, "n": 2318},
}

# The real 2026-10-01T22:27:55Z artifact, cut down to one (window, horizon) cell per
# segment and the first two ic_series points.
_RATING_PERFORMANCE = {
    "as_of_utc": "2026-10-01T22:27:55Z",
    "horizons": [1, 5, 20],
    "ic_series": [
        {"date": "2025-09-11", "horizon": 20, "ic": 0.075952},
        {"date": "2025-09-12", "horizon": 20, "ic": 0.112255},
    ],
    "rating_version": 2,
    "schema_version": 1,
    "segments": {
        "all": {"60": {"5": {
            "buckets": _BUCKETS_ALL_60_5,
            "ic_mean": -0.050401, "ic_n_dates": 60, "noise_floor_ic": 0.002483,
            "spread_strong_buy_minus_strong_sell": -0.004534,
        }}},
        "live": {"60": {"5": {
            "buckets": _BUCKETS_LIVE_60_5,
            "ic_mean": 0.072705, "ic_n_dates": 9, "noise_floor_ic": 2.8e-05,
            "spread_strong_buy_minus_strong_sell": 0.011275,
        }}},
    },
    "windows": [20, 60, 250],
}


def test_rating_performance_fixture_validates_and_reader_extracts_cells():
    _validate(_RATING_PERFORMANCE, "rating_performance")
    parsed = rating_performance_svc.load_rating_performance(
        reader=lambda: copy.deepcopy(_RATING_PERFORMANCE)
    )
    assert parsed is not None
    assert parsed.schema_version == 1
    assert parsed.horizons == [1, 5, 20] and parsed.windows == [20, 60, 250]
    stats = parsed.stats_for(segment="all", window=60, horizon=5)
    assert stats is not None
    assert stats.ic_mean == pytest.approx(-0.050401)
    assert set(stats.buckets) == {"Strong Sell", "Sell", "Neutral", "Buy", "Strong Buy"}
    live_buy = parsed.bucket_for_label("Buy", segment="live", window=60, horizon=5)
    assert live_buy is not None and live_buy.n == 1426
    assert [p.ic for p in parsed.ic_series] == pytest.approx([0.075952, 0.112255])


def test_rating_performance_missing_label_bucket_fails_schema():
    art = copy.deepcopy(_RATING_PERFORMANCE)
    del art["segments"]["all"]["60"]["5"]["buckets"]["Neutral"]
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "rating_performance")


def test_rating_performance_ic_outside_unit_interval_fails_schema():
    art = copy.deepcopy(_RATING_PERFORMANCE)
    art["ic_series"][0]["ic"] = 7.5952  # an IC is a correlation, so it lies in [-1, 1]
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "rating_performance")


# ── D48: market_data/index_contributions/{index}/{date}.json ─────────────────────────

_TRADING_DAY = date(2026, 9, 21)

_INDEX_CONTRIBUTIONS = {
    "schema_version": 1,
    "index": "SPX",
    "index_label": "S&P 500",
    "proxy_symbol": "SPY",
    "trading_day": _TRADING_DAY.isoformat(),
    "prior_close_date": "2026-09-18",
    "index_return_pct": 1.2,
    "weight_method": "official",
    "weights_as_of": "2026-09-18",
    # explained_pp = 0.14 + 0.288 = 0.428; residual_pp = 1.2 - 0.428 = 0.772.
    "explained_pp": 0.428,
    "residual_pp": 0.772,
    "coverage": {"weight_with_return": 0.08, "members": 3, "members_missing_return": 1},
    "members_missing_return": ["BRK.B"],
    "constituents": [
        # weight_prior_close (FRACTION) * return_pct (PERCENT) == contribution_pp (pp).
        {"symbol": "AAPL", "weight_prior_close": 0.07, "return_pct": 2.0, "contribution_pp": 0.14},
        {"symbol": "APP", "weight_prior_close": 0.01, "return_pct": 28.8, "contribution_pp": 0.288},
    ],
    "fetched_at": "2026-09-21T21:05:00Z",
}


def test_index_contributions_fixture_validates_and_spine_reader_parses_it():
    _validate(_INDEX_CONTRIBUTIONS, "index_contributions")
    dated_key = f"market_data/index_contributions/SPX/{_TRADING_DAY.isoformat()}.json"
    s3 = _s3_serving({dated_key: _INDEX_CONTRIBUTIONS})
    art = fetch_index_contributions(
        "SPX",
        _TRADING_DAY,
        source=lambda index, as_of: index_contributions_spine.spine_index_contributions(
            index, as_of, s3=s3
        ),
    )
    assert art is not None
    assert art.trading_day == _TRADING_DAY
    assert art.proxy_symbol == "SPY"
    assert art.index_return_fraction == pytest.approx(0.012)
    assert art.coverage_members_missing_return == 1
    by_symbol = {c.symbol: c for c in art.constituents}
    # The unit boundary the producer schema states: only weight_prior_close is a fraction.
    assert by_symbol["APP"].contribution_fraction == pytest.approx(0.00288)
    for c in art.constituents:
        assert c.weight_prior_close * c.return_pct == pytest.approx(c.contribution_pp)


def test_index_contributions_latest_fallback_is_read_only_for_the_same_day():
    latest_key = "market_data/index_contributions/SPX/latest.json"
    s3 = _s3_serving({latest_key: _INDEX_CONTRIBUTIONS})
    assert index_contributions_spine.spine_index_contributions("SPX", _TRADING_DAY, s3=s3) is not None
    assert index_contributions_spine.spine_index_contributions("SPX", date(2026, 9, 22), s3=s3) is None


def test_index_contributions_percent_weight_fails_schema():
    # 7.0 is AAPL's weight written as a percent. The schema's [0, 1] bound on
    # weight_prior_close is what catches this units drift.
    art = copy.deepcopy(_INDEX_CONTRIBUTIONS)
    art["constituents"][0]["weight_prior_close"] = 7.0
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "index_contributions")


def test_index_contributions_without_trading_day_fails_schema():
    art = copy.deepcopy(_INDEX_CONTRIBUTIONS)
    del art["trading_day"]
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, "index_contributions")
