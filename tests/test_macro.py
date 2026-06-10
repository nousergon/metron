"""Macro market context from FRED (C2-6d).

Injected source (never the network): a snapshot builds latest value + change-vs-prior
+ recent history per indicator; with no API key it reports unavailable WITH a reason;
an indicator the source omits is absent, never fabricated.
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
        s = macro.macro_snapshot(api_key="test", source=_source_first_two)
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

    def test_unavailable_without_api_key(self):
        s = macro.macro_snapshot(api_key="", source=_source_first_two)
        assert s.available is False and "FRED" in s.reason

    def test_unavailable_when_source_returns_nothing(self):
        s = macro.macro_snapshot(api_key="test", source=lambda inds, key: {})
        assert s.available is False and s.reason


class TestMacroEndpoint:
    def test_get_macro(self, client, monkeypatch):
        monkeypatch.setattr("api.services.macro.settings.fred_api_key", "test")
        monkeypatch.setattr("api.services.macro.fetch_macro_series", lambda inds, key, *, source=None: _source_first_two(inds, key))
        body = client.get("/macro").json()
        assert body["available"] is True
        assert len(body["indicators"]) == 2
        assert body["indicators"][0]["change"] == 1.5

    def test_get_macro_not_configured(self, client, monkeypatch):
        monkeypatch.setattr("api.services.macro.settings.fred_api_key", None)
        body = client.get("/macro").json()
        assert body["available"] is False and body["reason"]
