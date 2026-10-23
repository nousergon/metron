"""Runtime admission at the data-spine readers (metron-ops-I373).

`tests/test_p07_contracts.py` proves the pinned schemas reject bad payloads, but its
negative cases are validated by the TEST helper, never passed through the runtime
readers. These cases go through the REAL readers: an unknown or missing
``schema_version`` must be refused whole (read as absent, with an explicit reason), and a
present-but-non-finite number (NaN / Infinity / garbage) must never become a metric.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date
from unittest.mock import MagicMock

import pytest

from api.services import fundamentals, technicals
from portfolio_analytics.admission import Invalid, admit_schema, read_number
from portfolio_analytics.prices import ClosePoint, spine_source


def _s3_with(objects: dict[str, dict | None]) -> MagicMock:
    s3 = MagicMock()

    def _get(*, Bucket, Key):  # noqa: N803 - boto3 kwarg names
        if Key not in objects or objects[Key] is None:
            raise Exception(f"NoSuchKey: {Key}")
        body = MagicMock()
        # allow_nan (the default) writes NaN/Infinity tokens, which json.loads reads back
        # as floats — the same bytes a Python producer emits for a non-finite value.
        body.read.return_value = json.dumps(objects[Key]).encode()
        return {"Body": body}

    s3.get_object.side_effect = _get
    return s3


# ── the shared primitives ───────────────────────────────────────────────────


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "nan", "Infinity", "abc", True, [1]])
def test_a_present_non_finite_or_non_numeric_value_is_invalid_not_absent(value):
    assert isinstance(read_number(value), Invalid)


@pytest.mark.parametrize(("value", "expected"), [(None, None), (1, 1.0), ("2.5", 2.5), (0.0, 0.0), (-3, -3.0)])
def test_absent_and_finite_values_read_as_themselves(value, expected):
    assert read_number(value) == expected


@pytest.mark.parametrize("version", [999, 0, None, "5", 5.0, True])
def test_admit_schema_refuses_any_version_but_the_pinned_one(version, caplog):
    art = {"schema_version": version} if version is not None else {}
    with caplog.at_level(logging.ERROR, logger="portfolio_analytics.admission"):
        reason = admit_schema(art, expected=5, artifact="k.json")
    assert reason is not None and "k.json" in reason
    assert any("refused" in r.message for r in caplog.records), "a refusal must reach the ERROR log"


def test_admit_schema_passes_the_pinned_version_and_leaves_absence_to_the_caller():
    assert admit_schema({"schema_version": 5}, expected=5, artifact="k.json") is None
    assert admit_schema(None, expected=5, artifact="k.json") is None
    assert admit_schema(["not", "a", "dict"], expected=5, artifact="k.json") is not None  # type: ignore[arg-type]


# ── fundamentals ───────────────────────────────────────────────────────────


def test_the_audit_probe_unknown_schema_with_nan_is_refused_whole():
    """The exact offline probe that reproduced MET-05: schema 999 + trailingPE "nan"
    produced an AAPL record with a NaN P/E. It must produce nothing, and say why."""
    snap = fundamentals.load_fundamentals(reader=lambda: {
        "schema_version": 999,
        "as_of": "2026-10-22",
        "fundamentals": {"AAPL": {"trailingPE": "nan"}},
    })
    assert snap.by_symbol == {}
    assert snap.as_of is None
    assert snap.schema_version == 999
    assert snap.rejected_reason is not None and "999" in snap.rejected_reason


def test_an_unversioned_fundamentals_artifact_is_refused():
    snap = fundamentals.load_fundamentals(reader=lambda: {"as_of": "2026-10-22", "fundamentals": {"AAPL": {"trailingPE": 30.0}}})
    assert snap.by_symbol == {}
    assert snap.rejected_reason is not None


def test_non_finite_fundamentals_are_unavailable_and_named_never_metrics():
    snap = fundamentals.load_fundamentals(reader=lambda: {
        "schema_version": fundamentals.FUNDAMENTALS_SCHEMA_VERSION,
        "as_of": "2026-10-22",
        "source": "yfinance",
        "fundamentals": {"AAPL": {
            "trailingPE": "nan", "earningsGrowth": 0.1, "forwardPE": float("inf"),
            "marketCap": 3.2e12, "beta": None,
        }},
    })
    aapl = snap.by_symbol["AAPL"]
    assert aapl.trailing_pe is None and aapl.forward_pe is None
    assert aapl.peg is None, "PEG must not be derived from a NaN P/E"
    assert aapl.market_cap == 3.2e12
    assert aapl.beta is None
    # Invalid is distinct from absent: beta was null (absent) and is not listed.
    assert snap.invalid_fields == {"AAPL": ("trailingPE", "forwardPE")}
    for value in vars(aapl).values():
        assert not (isinstance(value, float) and not math.isfinite(value))
    # Lineage is retained.
    assert (snap.schema_version, snap.source, snap.as_of) == (5, "yfinance", date(2026, 10, 22))
    assert snap.rejected_reason is None


def test_an_absent_fundamentals_artifact_is_still_empty_and_not_a_refusal():
    snap = fundamentals.load_fundamentals(reader=lambda: None)
    assert snap.by_symbol == {} and snap.rejected_reason is None


# ── technicals ─────────────────────────────────────────────────────────────


def test_an_unknown_technicals_schema_is_refused_whole():
    snap = technicals.load_technicals(reader=lambda: {
        "schema_version": 999, "as_of": "2026-10-22", "technicals": {"AAPL": {"rsi_14": 55.0}},
    })
    assert snap.by_symbol == {} and snap.rejected_reason is not None


def test_non_finite_technicals_are_unavailable_and_named():
    snap = technicals.load_technicals(reader=lambda: {
        "schema_version": technicals.TECHNICALS_SCHEMA_VERSION,
        "as_of": "2026-10-22",
        "source": "computed",
        "technicals": {"AAPL": {"rsi_14": float("nan"), "mom_20d": "Infinity", "ma_50": 190.0}},
    })
    aapl = snap.by_symbol["AAPL"]
    assert aapl.rsi_14 is None and aapl.mom_20d is None and aapl.ma_50 == 190.0
    assert snap.invalid_fields == {"AAPL": ("rsi_14", "mom_20d")}
    assert (snap.schema_version, snap.source) == (4, "computed")


# ── latest closes / FX ─────────────────────────────────────────────────────

_CLOSES = {"schema_version": 1, "as_of": "2026-10-22", "source": "alpha-engine-data",
           "closes": {"AAPL": {"close": 201.5, "currency": "USD", "bar_date": "2026-10-22"}}}
_FX = {"schema_version": 1, "as_of": "2026-10-22", "base": "USD", "rates": {"HKD": 0.1282}}


def test_an_unknown_closes_schema_prices_nothing():
    s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: {**_CLOSES, "schema_version": 999},
                   spine_source.FX_LATEST_KEY: _FX})
    out = spine_source.spine_latest_closes(["AAPL", "HKDUSD=X"], s3=s3)
    assert "AAPL" not in out
    assert out["HKDUSD=X"].close == pytest.approx(0.1282)


def test_an_unversioned_fx_artifact_converts_nothing():
    s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: _CLOSES,
                   spine_source.FX_LATEST_KEY: {k: v for k, v in _FX.items() if k != "schema_version"}})
    out = spine_source.spine_latest_closes(["AAPL", "HKDUSD=X"], s3=s3)
    assert set(out) == {"AAPL"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan", "abc", 0, -1.0])
def test_a_non_finite_or_non_positive_latest_close_or_rate_is_omitted(bad):
    closes = {**_CLOSES, "closes": {"AAPL": {"close": bad, "currency": "USD", "bar_date": "2026-10-22"}}}
    fx = {**_FX, "rates": {"HKD": bad}}
    s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: closes, spine_source.FX_LATEST_KEY: fx})
    assert spine_source.spine_latest_closes(["AAPL", "HKDUSD=X"], s3=s3) == {}


# ── history ────────────────────────────────────────────────────────────────

_GOOD_ROWS = [["2026-10-20", 198.0], ["2026-10-21", 200.0], ["2026-10-22", 201.5]]


def _ticker(rows, **overrides):
    return {"schema_version": 1, "yf_symbol": "AAPL", "currency": "USD",
            "adjustment_basis": "dividend_adjusted", "closes": rows, **overrides}


def _history(objects, symbols=("AAPL",)):
    return spine_source.spine_close_history(list(symbols), date(2026, 10, 1), date(2026, 10, 31), s3=_s3_with(objects))


@pytest.mark.parametrize("bad_row", [["2026-10-21", float("nan")], ["2026-10-21", float("inf")],
                                     ["2026-10-21", "x"], ["not-a-date", 200.0], ["2026-10-21"], "garbage",
                                     ["2026-10-21", 0.0]])
def test_one_bad_row_refuses_the_whole_series_rather_than_leaving_a_silent_hole(bad_row):
    rows = [_GOOD_ROWS[0], bad_row, _GOOD_ROWS[2]]
    assert _history({"market_data/close_history/AAPL.json": _ticker(rows)}) == {}


def test_a_bad_row_outside_the_window_still_refuses_the_series():
    """Corrupt input is corrupt wherever it sits: the series is one product."""
    rows = [["2025-01-02", float("nan")], *_GOOD_ROWS]
    assert _history({"market_data/close_history/AAPL.json": _ticker(rows)}) == {}


@pytest.mark.parametrize("overrides", [{"schema_version": 2}, {"schema_version": None},
                                       {"adjustment_basis": "split_only"}, {"adjustment_basis": None}])
def test_a_per_ticker_history_off_contract_is_refused(overrides):
    assert _history({"market_data/close_history/AAPL.json": _ticker(_GOOD_ROWS, **overrides)}) == {}


@pytest.mark.parametrize("overrides", [{"schema_version": 999}, {"adjustment_basis": "split_only"}])
def test_an_off_contract_consolidated_artifact_falls_back_to_admitted_per_ticker_files(overrides):
    consolidated = {"schema_version": 1, "adjustment_basis": "dividend_adjusted",
                    "series": {"AAPL": [["2026-10-22", 999.0]]}, "currency": {"AAPL": "USD"}, **overrides}
    out = _history({spine_source.CLOSE_HISTORY_CONSOLIDATED_KEY: consolidated,
                    "market_data/close_history/AAPL.json": _ticker(_GOOD_ROWS)})
    assert out["AAPL"][-1] == ClosePoint(date(2026, 10, 22), 201.5), "the refused consolidated value must not be read"


def test_an_unknown_fx_history_schema_is_refused():
    out = _history({"market_data/fx_history/HKD.json": {"schema_version": 7, "currency": "HKD", "base": "USD",
                                                       "rates": [["2026-10-22", 0.1282]]}},
                   symbols=("HKDUSD=X",))
    assert out == {}


def test_an_on_contract_series_is_read_unchanged():
    out = _history({"market_data/close_history/AAPL.json": _ticker(_GOOD_ROWS)})
    assert out["AAPL"] == [ClosePoint(date(2026, 10, 20), 198.0), ClosePoint(date(2026, 10, 21), 200.0),
                           ClosePoint(date(2026, 10, 22), 201.5)]
