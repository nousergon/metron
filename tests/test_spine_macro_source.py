"""Data-spine macro source — Metron's consumer side of the macro contract with
`alpha-engine-data`. The producer writes `market_data/macro/latest.json` (keyed by FRED
series id); this verifies Metron maps it to per-indicator MacroSeries, fail-softs a
missing artifact, and that the dispatcher defaults to the spine (no FRED).
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from portfolio_analytics.macro import INDICATORS, fetch_macro_series
from portfolio_analytics.macro import spine_source as macro_spine


def _s3_with(obj: dict | None) -> MagicMock:
    s3 = MagicMock()

    def _get(*, Bucket, Key):  # noqa: N803
        if obj is None:
            raise Exception("NoSuchKey")
        body = MagicMock()
        body.read.return_value = json.dumps(obj).encode()
        return {"Body": body}

    s3.get_object.side_effect = _get
    return s3


_MACRO = {"schema_version": 1, "as_of": "2026-06-11", "series": {
    "FEDFUNDS": [["2026-05-01", 5.33], ["2026-06-01", 5.33]],
    "VIXCLS": [["2026-06-10", 14.2], ["2026-06-11", 13.8]],
}}


def test_maps_series_to_indicator_keys():
    s3 = _s3_with(_MACRO)
    out = macro_spine.spine_macro_series(INDICATORS, s3=s3)
    # FEDFUNDS → fed_funds key; VIXCLS → vix key; others absent from the artifact.
    assert set(out) == {"fed_funds", "vix"}
    assert out["fed_funds"].observations[-1] == macro_spine.MacroObservation(date(2026, 6, 1), 5.33)
    assert out["vix"].observations[-1].value == 13.8


def test_missing_artifact_fail_soft():
    s3 = _s3_with(None)
    assert macro_spine.spine_macro_series(INDICATORS, s3=s3) == {}


def test_fetch_macro_series_defaults_to_spine(monkeypatch):
    from portfolio_analytics.macro.source import MacroObservation, MacroSeries
    monkeypatch.setattr(macro_spine, "spine_macro_series",
                        lambda inds, key="", **k: {"vix": MacroSeries([MacroObservation(date(2026, 6, 11), 13.8)])})
    out = fetch_macro_series(INDICATORS)  # no source, no api_key → spine default
    assert out["vix"].observations[0].value == 13.8


def test_empty_indicator_set():
    assert fetch_macro_series([]) == {}
