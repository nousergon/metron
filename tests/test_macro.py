"""Macro market context from the data spine (C2-6d → C2-6o-fu-macro).

Injected source (never the network): a snapshot builds latest value + change-vs-prior
+ recent history per indicator; when the spine has no macro data it reports unavailable
WITH a reason; an indicator the source omits is absent, never fabricated.
"""

from __future__ import annotations

from datetime import date

from api.services import macro
from portfolio_analytics.macro import INDICATORS, MacroObservation, MacroSeries


def _series(vals: list[tuple[str, float]]) -> MacroSeries:
    return MacroSeries([MacroObservation(date.fromisoformat(d), v) for d, v in vals])


def _source_first_two(indicators, api_key):
    # Only the first two indicators resolve — the rest are "missing from FRED".
    out = {}
    for ind in indicators[:2]:
        out[ind.key] = _series([("2024-01-01", 3.0), ("2024-02-01", 4.0), ("2024-03-01", 5.5)])
    return out


class TestMacroSnapshot:
    def test_builds_indicators_with_change(self):
        s = macro.macro_snapshot(source=_source_first_two)
        assert s.available is True
        assert len(s.indicators) == 2  # only the two the source returned
        first = s.indicators[0]
        assert first.latest_value == 5.5
        assert first.prior_value == 4.0
        assert first.change == 0.5 + 1.0  # 5.5 − 4.0
        assert first.latest_date == date(2024, 3, 1)
        assert first.history[0].obs_date == date(2024, 3, 1)  # most recent first
        assert s.as_of == date(2024, 3, 1)
        # Labels/units come from the curated INDICATORS, in order.
        assert first.label == INDICATORS[0].label and first.units == INDICATORS[0].units

    def test_unavailable_when_spine_has_no_macro(self):
        s = macro.macro_snapshot(source=lambda inds, key="": {})
        assert s.available is False and s.reason


class TestMacroEndpoint:
    def test_get_macro(self, client, monkeypatch):
        monkeypatch.setattr("api.services.macro.fetch_macro_series",
                            lambda inds, key="", *, source=None: _source_first_two(inds, key))
        body = client.get("/macro").json()
        assert body["available"] is True
        assert len(body["indicators"]) == 2
        assert body["indicators"][0]["change"] == 1.5

    def test_get_macro_unavailable_when_spine_empty(self, client, monkeypatch):
        monkeypatch.setattr("api.services.macro.fetch_macro_series", lambda inds, key="", *, source=None: {})
        body = client.get("/macro").json()
        assert body["available"] is False and body["reason"]


def _daily(n: int) -> list[tuple[str, float]]:
    """n ascending daily observations from a fixed base (for history-depth tests)."""
    from datetime import date, timedelta

    base = date(2023, 1, 1)
    return [((base + timedelta(days=i)).isoformat(), float(i)) for i in range(n)]


class TestMacroHistoryDepth:
    @staticmethod
    def _src(n: int):
        def source(indicators, api_key=""):
            return {indicators[0].key: _series(_daily(n))}
        return source

    def test_default_caps_history_lean(self):
        s = macro.macro_snapshot(source=self._src(60))
        assert len(s.indicators[0].history) == 24  # default lean window (most recent first)
        assert s.indicators[0].history[0].value == 59.0  # newest first

    def test_full_limit_returns_deep_history(self):
        s = macro.macro_snapshot(source=self._src(300), history_limit=macro.FULL_HISTORY_LIMIT)
        assert len(s.indicators[0].history) == 300  # all of them (< FULL_HISTORY_LIMIT)

    def test_endpoint_full_param_deepens_history(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.services.macro.fetch_macro_series",
            lambda inds, key="", *, source=None: {inds[0].key: _series(_daily(300))},
        )
        short = client.get("/macro").json()
        full = client.get("/macro?full=true").json()
        assert len(short["indicators"][0]["history"]) == 24
        assert len(full["indicators"][0]["history"]) == 300
