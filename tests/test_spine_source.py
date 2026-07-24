"""Data-spine price/FX source — Metron's consumer side of the contract with
`alpha-engine-data`. The producer writes the artifacts (see
alpha-engine-data/collectors/metron_market_data.py); this verifies Metron reads them
into `ClosePoint`s, serves FX-pair symbols from the FX artifact, filters history to the
window, and fail-softs a missing artifact (cost-basis fallback, never fabrication).
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from portfolio_analytics.prices import ClosePoint, fetch_close_history, fetch_latest_closes, spine_source


def _s3_with(objects: dict[str, dict | None]) -> MagicMock:
    """A MagicMock S3 serving ``objects`` (key → JSON dict, or None → raise NoSuchKey)."""
    s3 = MagicMock()

    def _get(*, Bucket, Key):  # noqa: N803 - boto3 kwarg names
        if Key not in objects or objects[Key] is None:
            raise Exception(f"NoSuchKey: {Key}")
        body = MagicMock()
        body.read.return_value = json.dumps(objects[Key]).encode()
        return {"Body": body}

    s3.get_object.side_effect = _get
    return s3


_CLOSES = {"schema_version": 1, "as_of": "2026-06-11", "source": "alpha-engine-data",
           "closes": {"AAPL": {"close": 201.5, "currency": "USD", "bar_date": "2026-06-11"},
                      "1299.HK": {"close": 64.2, "currency": "HKD", "bar_date": "2026-06-11"}}}
_FX = {"schema_version": 1, "as_of": "2026-06-11", "base": "USD", "rates": {"HKD": 0.1282}}


class TestLatest:
    def test_equity_and_fx_pair_resolve_from_their_artifacts(self):
        s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: _CLOSES, spine_source.FX_LATEST_KEY: _FX})
        out = spine_source.spine_latest_closes(["AAPL", "1299.HK", "HKDUSD=X"], s3=s3)
        assert out["AAPL"] == ClosePoint(bar_date=date(2026, 6, 11), close=201.5)
        assert out["1299.HK"] == ClosePoint(bar_date=date(2026, 6, 11), close=64.2)
        # FX pair served from the fx artifact, rate as a ClosePoint dated to the artifact.
        assert out["HKDUSD=X"] == ClosePoint(bar_date=date(2026, 6, 11), close=0.1282)

    def test_unknown_symbol_omitted_not_fabricated(self):
        s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: _CLOSES, spine_source.FX_LATEST_KEY: _FX})
        out = spine_source.spine_latest_closes(["AAPL", "NOTHELD", "EURUSD=X"], s3=s3)
        assert set(out) == {"AAPL"}  # NOTHELD absent from closes; EUR absent from fx rates

    def test_missing_artifact_fail_soft_returns_empty(self):
        s3 = _s3_with({spine_source.CLOSES_LATEST_KEY: None, spine_source.FX_LATEST_KEY: None})
        assert spine_source.spine_latest_closes(["AAPL", "HKDUSD=X"], s3=s3) == {}


class TestHistory:
    def test_close_and_fx_history_filtered_to_window(self):
        s3 = _s3_with({
            "market_data/close_history/AAPL.json": {"closes": [["2026-06-09", 198.0], ["2026-06-10", 200.0], ["2026-06-11", 201.5]]},
            "market_data/fx_history/HKD.json": {"rates": [["2026-06-10", 0.128], ["2026-06-11", 0.1282]]},
        })
        out = spine_source.spine_close_history(["AAPL", "HKDUSD=X"], date(2026, 6, 10), date(2026, 6, 11), s3=s3)
        assert out["AAPL"] == [ClosePoint(date(2026, 6, 10), 200.0), ClosePoint(date(2026, 6, 11), 201.5)]
        assert out["HKDUSD=X"] == [ClosePoint(date(2026, 6, 10), 0.128), ClosePoint(date(2026, 6, 11), 0.1282)]

    def test_missing_history_artifact_omitted(self):
        s3 = _s3_with({"market_data/close_history/AAPL.json": None})
        assert spine_source.spine_close_history(["AAPL"], date(2026, 1, 1), date(2026, 12, 31), s3=s3) == {}

    def test_consolidated_close_history_reads_one_file(self):
        # metron-ops#233: when the consolidated artifact exists, spine_close_history loads
        # it once and filters in memory rather than reading N per-ticker files.
        s3 = _s3_with({
            spine_source.CLOSE_HISTORY_CONSOLIDATED_KEY: {
                "schema_version": 1,
                "series": {
                    "AAPL": [["2026-06-09", 198.0], ["2026-06-10", 200.0], ["2026-06-11", 201.5]],
                    "MSFT": [["2026-06-10", 350.0], ["2026-06-11", 355.0]],
                },
                "currency": {"AAPL": "USD", "MSFT": "USD"},
            },
        })
        out = spine_source.spine_close_history(["AAPL", "MSFT"], date(2026, 6, 10), date(2026, 6, 11), s3=s3)
        assert out["AAPL"] == [ClosePoint(date(2026, 6, 10), 200.0), ClosePoint(date(2026, 6, 11), 201.5)]
        assert out["MSFT"] == [ClosePoint(date(2026, 6, 10), 350.0), ClosePoint(date(2026, 6, 11), 355.0)]

    def test_consolidated_missing_falls_back_to_per_ticker_files(self):
        # Without the consolidated artifact, spine_close_history degrades to per-ticker
        # reads (transition period or producer lag).
        s3 = _s3_with({
            "market_data/close_history/AAPL.json": {"closes": [["2026-06-09", 198.0], ["2026-06-10", 200.0], ["2026-06-11", 201.5]]},
        })
        out = spine_source.spine_close_history(["AAPL"], date(2026, 6, 10), date(2026, 6, 11), s3=s3)
        assert out["AAPL"] == [ClosePoint(date(2026, 6, 10), 200.0), ClosePoint(date(2026, 6, 11), 201.5)]

    def test_consolidated_does_not_break_fx_pairs(self):
        # FX pairs must still resolve from per-currency FX history even when the
        # consolidated equity file is present (FX is never consolidated into it).
        s3 = _s3_with({
            spine_source.CLOSE_HISTORY_CONSOLIDATED_KEY: {
                "schema_version": 1,
                "series": {"AAPL": [["2026-06-11", 201.5]]},
                "currency": {"AAPL": "USD"},
            },
            "market_data/fx_history/HKD.json": {"rates": [["2026-06-11", 0.1282]]},
        })
        out = spine_source.spine_close_history(["AAPL", "HKDUSD=X"], date(2026, 6, 11), date(2026, 6, 11), s3=s3)
        assert out["AAPL"] == [ClosePoint(date(2026, 6, 11), 201.5)]
        assert out["HKDUSD=X"] == [ClosePoint(date(2026, 6, 11), 0.1282)]


class TestDispatcherDefaultsToSpine:
    def test_fetch_latest_closes_uses_spine_by_default(self, monkeypatch):
        called = {}
        def _fake_spine(symbols, *, s3=None):
            called["symbols"] = symbols
            return {"AAPL": ClosePoint(date(2026, 6, 11), 201.5)}
        monkeypatch.setattr(spine_source, "spine_latest_closes", _fake_spine)
        out = fetch_latest_closes(["AAPL", "AAPL", ""])  # no source injected → spine default
        assert out == {"AAPL": ClosePoint(date(2026, 6, 11), 201.5)}
        assert called["symbols"] == ["AAPL"]

    def test_injected_source_bypasses_spine(self):
        out = fetch_latest_closes(["X"], source=lambda s: {"X": ClosePoint(date(2026, 1, 1), 5.0)})
        assert out["X"].close == 5.0

    def test_fetch_close_history_uses_spine_by_default(self, monkeypatch):
        monkeypatch.setattr(spine_source, "spine_close_history",
                            lambda syms, s, e, **k: {"AAPL": [ClosePoint(date(2026, 6, 11), 201.5)]})
        out = fetch_close_history(["AAPL"], date(2026, 1, 1), date(2026, 12, 31))
        assert out["AAPL"][0].close == 201.5
