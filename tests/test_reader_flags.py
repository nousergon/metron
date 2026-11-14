"""Closes-when probes for metron-ops-I373: an empty or collapsed universe, and stale content,
through the real readers and out to the API responses (``stale_reasons`` / ``universe_reasons``)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.config import settings
from api.routers.portfolios import TearsheetOut
from api.services import (
    analyst,
    fundamentals,
    security_performance,
    sentiment,
    tearsheet,
    technical_rating,
    technicals,
    valuation_medians,
)
from portfolio_analytics.admission import reader_flags, universe_shortfall
from tests.test_api_input_source import _seed_http
from tests.test_tearsheet import _PERF_ART, _TECH_ART, _seed

_NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
_TODAY = "2026-11-13"
_OLD = "2026-01-02"


def test_universe_shortfall():
    assert universe_shortfall(1, artifact="a") is None
    assert universe_shortfall(0, artifact="a") is not None
    reason = universe_shortfall(5, artifact="a", minimum=100)
    assert reason is not None and "5 symbol" in reason and "100" in reason


# ── (1) empty / far-too-small universe, through the real readers ─────────────


@pytest.mark.parametrize(
    ("load", "art"),
    [
        (fundamentals.load_fundamentals, {"schema_version": 5, "as_of": _TODAY, "fundamentals": {}}),
        (analyst.load_analyst, {"schema_version": 1, "as_of": _TODAY, "analyst": {}}),
        (sentiment.load_sentiment, {"schema_version": 1, "as_of": _TODAY, "sentiment": {}}),
        (technicals.load_technicals, {"schema_version": 4, "as_of": _TODAY, "technicals": {}}),
        (
            security_performance.load_security_performance,
            {"schema_version": 1, "as_of": _TODAY, "performance": {}},
        ),
        (
            valuation_medians.load_valuation_medians,
            {"schema_version": 1, "as_of": _TODAY, "by_sector": {}, "by_country": {}},
        ),
    ],
)
def test_empty_symbol_map_carries_a_universe_reason(load, art):
    snap = load(reader=lambda: art)
    assert snap.universe_reason is not None
    assert "0 symbol" in snap.universe_reason or "holds 0" in snap.universe_reason


def test_absent_artifact_is_not_an_empty_universe_nor_stale():
    # A failed read is the fail-soft absent case, reported by absence, not by a flag.
    for snap in (
        fundamentals.load_fundamentals(reader=lambda: None),
        technicals.load_technicals(reader=lambda: None),
        analyst.load_analyst(reader=lambda: None),
        sentiment.load_sentiment(reader=lambda: None),
        security_performance.load_security_performance(reader=lambda: None),
        valuation_medians.load_valuation_medians(reader=lambda: None),
    ):
        assert snap.universe_reason is None
        assert snap.stale_reason is None


def test_broad_products_flag_a_collapsed_universe_not_just_an_empty_one():
    small = {f"S{i}": {"rsi_14": 50.0} for i in range(technicals.MIN_UNIVERSE - 1)}
    snap = technicals.load_technicals(
        reader=lambda: {"schema_version": 4, "as_of": _TODAY, "technicals": small}, now=_NOW
    )
    assert snap.universe_reason is not None
    full = {f"S{i}": {"rsi_14": 50.0} for i in range(technicals.MIN_UNIVERSE)}
    ok = technicals.load_technicals(
        reader=lambda: {"schema_version": 4, "as_of": _TODAY, "technicals": full}, now=_NOW
    )
    assert ok.universe_reason is None


def test_technicals_producer_declared_coverage_below_floor_is_flagged():
    full = {f"S{i}": {"rsi_14": 50.0} for i in range(technicals.MIN_UNIVERSE)}
    art = {
        "schema_version": 4,
        "as_of": _TODAY,
        "technicals": full,
        "coverage": {"meets_floor": False, "covered": 500, "denominator": 936},
    }
    snap = technicals.load_technicals(reader=lambda: art, now=_NOW)
    assert snap.universe_reason is not None and "500" in snap.universe_reason and "936" in snap.universe_reason
    assert len(snap.by_symbol) == technicals.MIN_UNIVERSE  # served as before; flagged, not refused


def test_a_refused_artifact_has_no_universe_reason():
    snap = fundamentals.load_fundamentals(reader=lambda: {"schema_version": 999})
    assert snap.rejected_reason is not None and snap.universe_reason is None


# ── staleness on the other spine readers ─────────────────────────────────────


@pytest.mark.parametrize(
    ("load", "key", "version"),
    [
        (analyst.load_analyst, "analyst", 1),
        (sentiment.load_sentiment, "sentiment", 1),
        (security_performance.load_security_performance, "performance", 1),
    ],
)
def test_other_spine_readers_flag_stale_content(load, key, version):
    body = {"AAPL": {}}
    stale = load(reader=lambda: {"schema_version": version, "as_of": _OLD, key: body}, now=_NOW)
    assert stale.stale_reason is not None and _OLD in stale.stale_reason
    assert "AAPL" in stale.by_symbol  # a flag: the numbers are still served
    fresh = load(reader=lambda: {"schema_version": version, "as_of": _TODAY, key: body}, now=_NOW)
    assert fresh.stale_reason is None


def test_valuation_medians_stale_uses_the_weekly_limit():
    def art(as_of: str) -> dict:
        return {"schema_version": 1, "as_of": as_of, "by_sector": {"Tech": {}}, "by_country": {}}

    assert valuation_medians.load_valuation_medians(reader=lambda: art("2026-11-06"), now=_NOW).stale_reason is None
    snap = valuation_medians.load_valuation_medians(reader=lambda: art("2026-10-20"), now=_NOW)
    assert snap.stale_reason is not None


def test_reader_flags_collects_only_set_reasons():
    stale_snap = analyst.load_analyst(
        reader=lambda: {"schema_version": 1, "as_of": _OLD, "analyst": {"A": {}}}, now=_NOW
    )
    ok_snap = analyst.load_analyst(
        reader=lambda: {"schema_version": 1, "as_of": _TODAY, "analyst": {"A": {}}}, now=_NOW
    )
    assert reader_flags({"x": stale_snap, "y": ok_snap}) == ({"x": stale_snap.stale_reason}, {})


# ── (3) surfaced in API responses ─────────────────────────────────────────────

_STALE_FUND = {"schema_version": 5, "as_of": _OLD, "fundamentals": {"AAPL": {"sector": "Tech"}}}
_STALE_TECH = {"schema_version": 4, "as_of": _OLD, "technicals": {"AAPL": {"rsi_14": 50.0}}}
_EMPTY_ANALYST = {"schema_version": 1, "as_of": _TODAY, "analyst": {}}


def _patch(monkeypatch):
    monkeypatch.setattr(fundamentals, "_default_read", lambda: (_STALE_FUND, None))
    monkeypatch.setattr(technicals, "_default_read", lambda: (_STALE_TECH, None))
    monkeypatch.setattr(analyst, "_default_read", lambda: (_EMPTY_ANALYST, None))
    monkeypatch.setattr(sentiment, "_default_read", lambda: (None, None))
    monkeypatch.setattr(technical_rating, "_default_intraday_read", lambda: (None, None))
    monkeypatch.setattr(technical_rating, "_default_technicals_read", lambda: (None, None))


def _assert_flags(row: dict) -> None:
    assert set(row["stale_reasons"]) == {"fundamentals", "technicals"}
    assert _OLD in row["stale_reasons"]["fundamentals"]
    assert set(row["universe_reasons"]) >= {"analyst", "technicals"}  # technicals: 1 symbol < minimum
    assert "sentiment" not in row["stale_reasons"] and "sentiment" not in row["universe_reasons"]


def test_holdings_endpoint_serves_the_flags(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch(monkeypatch)
    pid, hdr = _seed_http(client)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=hdr).json()
    _assert_flags(next(r for r in rows if r["ticker"] == "AAPL"))


def test_watchlist_endpoint_serves_the_flags(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch(monkeypatch)
    pid, hdr = _seed_http(client)
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "NVDA"}, headers=hdr)
    rows = client.get(f"/portfolios/{pid}/watchlist", headers=hdr).json()
    _assert_flags(next(r for r in rows if r["symbol"] == "NVDA"))


def test_holdings_endpoint_off_feed_has_no_flags(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)
    pid, hdr = _seed_http(client)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=hdr).json()
    assert rows and all(r["stale_reasons"] == {} and r["universe_reasons"] == {} for r in rows)


def test_valuation_medians_endpoint_serves_the_flags(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    monkeypatch.setattr(
        valuation_medians,
        "_default_read",
        lambda: ({"schema_version": 1, "as_of": _OLD, "by_sector": {}, "by_country": {}}, None),
    )
    pid, hdr = _seed_http(client)
    body = client.get(f"/portfolios/{pid}/valuation-medians", headers=hdr).json()
    assert _OLD in body["stale_reason"] and "by_sector" not in body["universe_reason"]
    assert "holds 0" in body["universe_reason"]


def test_tearsheet_serves_the_flags(db_session, monkeypatch):
    monkeypatch.setattr(security_performance, "_default_read", lambda: (_PERF_ART, None))
    monkeypatch.setattr(technicals, "_default_read", lambda: (_TECH_ART, None))
    monkeypatch.setattr(technical_rating, "_default_intraday_read", lambda: (None, None))
    monkeypatch.setattr(technical_rating, "_default_technicals_read", lambda: (_TECH_ART, None))
    monkeypatch.setattr(fundamentals, "_default_read", lambda: (_STALE_FUND, None))
    monkeypatch.setattr(analyst, "_default_read", lambda: (_EMPTY_ANALYST, None))
    monkeypatch.setattr(sentiment, "_default_read", lambda: (None, None))
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=True)
    assert sheet is not None
    assert _OLD in sheet.stale_reasons["fundamentals"]
    assert "analyst" in sheet.universe_reasons
    out = TearsheetOut.model_validate(sheet).model_dump(mode="json")
    assert out["stale_reasons"] == sheet.stale_reasons and out["universe_reasons"] == sheet.universe_reasons
