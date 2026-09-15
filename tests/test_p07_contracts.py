"""Consumer contract tests — data-collector plan P-07 (alpha-engine-config-I10774).

Pinned copies of nousergon-data/contracts/*.schema.json live in tests/contracts/ here
(mirrors the existing realized_lots / technical_ratings precedent — Metron never imports
nousergon-data; the versioned JSON schema IS the coupling). Each test:

  1. checks the pinned schema is itself a valid JSON Schema (guards a bad re-pin);
  2. builds a schema-conformant fixture and feeds it through the REAL consumer reader
     (injected via each service module's `reader=` seam) so a field this consumer
     actually depends on is exercised, not just declared;
  3. asserts a deliberately broken field/type fails schema validation — the drift alarm
     a pinned copy exists to provide.

If nousergon-data ships a new schema version, re-pin the file here and this test fails
loudly until the read matches.

Refs alpha-engine-config-I10774, data_collection_plan_260914.md §3/§4.3.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from api.services import analyst as analyst_svc
from api.services import fundamentals as fundamentals_svc
from api.services import intraday as intraday_svc
from api.services import security_performance as security_performance_svc
from api.services import sentiment as sentiment_svc
from api.services import valuation_medians as valuation_medians_svc
from portfolio_analytics.calendar import spine_source as calendar_spine
from portfolio_analytics.macro import spine_source as macro_spine
from portfolio_analytics.prices import spine_source as prices_spine
from portfolio_analytics.sectors import spine_source as sectors_spine

CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS_DIR / f"{name}.schema.json").read_text())


def _validate(payload: dict, schema: dict) -> None:
    jsonschema.validate(instance=payload, schema=schema)


@pytest.mark.parametrize("name", [
    "metron_closes", "metron_fx", "metron_close_history", "metron_fx_history",
    "metron_sectors", "metron_earnings", "metron_macro", "metron_fundamentals",
    "metron_security_performance", "metron_analyst", "metron_sentiment",
    "metron_valuation_medians", "metron_intraday_latest",
])
def test_pinned_schema_is_valid(name):
    jsonschema.Draft202012Validator.check_schema(_schema(name))


# ── eod_closes / fx (prices.spine_source) ───────────────────────────────────

def test_closes_fixture_validates_and_reader_extracts_close():
    schema = _schema("metron_closes")
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "alpha-engine-data",
           "closes": {"AAPL": {"close": 201.5, "currency": "USD", "bar_date": "2026-06-26"}}}
    _validate(art, schema)
    s3 = type("S3", (), {"get_object": staticmethod(
        lambda Bucket, Key: {"Body": type("B", (), {"read": lambda self: json.dumps(art).encode()})()}
    )})()
    closes = prices_spine.spine_latest_closes(["AAPL"], s3=s3)
    assert closes["AAPL"].close == pytest.approx(201.5)


def test_closes_missing_close_field_fails_schema():
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "alpha-engine-data",
           "closes": {"AAPL": {"currency": "USD", "bar_date": "2026-06-26"}}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_closes"))


def test_fx_fixture_validates():
    _validate({"schema_version": 1, "as_of": "2026-06-26", "base": "USD", "rates": {"EUR": 1.08}},
              _schema("metron_fx"))


# ── close_history / fx_history ───────────────────────────────────────────────

def test_close_history_fixture_validates_and_reader_extracts_series():
    schema = _schema("metron_close_history")
    art = {"schema_version": 1, "yf_symbol": "AAPL", "currency": "USD",
           "adjustment_basis": "dividend_adjusted",
           "closes": [["2026-06-25", 199.0], ["2026-06-26", 201.5]]}
    _validate(art, schema)

    def _get(Bucket, Key):
        if Key == prices_spine.CLOSE_HISTORY_CONSOLIDATED_KEY:
            raise Exception("NoSuchKey")  # force the per-symbol fallback path
        return {"Body": type("B", (), {"read": lambda self: json.dumps(art).encode()})()}

    s3 = type("S3", (), {"get_object": staticmethod(_get)})()
    from datetime import date
    series = prices_spine.spine_close_history(
        ["AAPL"], date(2026, 6, 25), date(2026, 6, 26), s3=s3,
    )
    assert series["AAPL"]


def test_close_history_wrong_pair_shape_fails_schema():
    art = {"schema_version": 1, "yf_symbol": "AAPL", "currency": "USD",
           "adjustment_basis": "dividend_adjusted", "closes": [["2026-06-26"]]}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_close_history"))


def test_fx_history_fixture_validates():
    _validate({"schema_version": 1, "currency": "EUR", "base": "USD",
               "rates": [["2026-06-26", 1.08]]}, _schema("metron_fx_history"))


# ── sectors / earnings ────────────────────────────────────────────────────────

def test_sectors_fixture_validates_and_reader_extracts_sector():
    schema = _schema("metron_sectors")
    art = {"schema_version": 2, "as_of": "2026-06-26",
           "sectors": {"AAPL": "Technology"}, "countries": {"AAPL": "United States"},
           "spy_sector_weights": {"Technology": 0.3}}
    _validate(art, schema)
    s3 = type("S3", (), {"get_object": staticmethod(
        lambda Bucket, Key: {"Body": type("B", (), {"read": lambda self: json.dumps(art).encode()})()}
    )})()
    sectors = sectors_spine.spine_sectors(["AAPL"], s3=s3)
    assert sectors["AAPL"] == "Technology"


def test_sectors_weight_over_one_fails_schema():
    art = {"schema_version": 2, "as_of": "2026-06-26", "sectors": {}, "countries": {},
           "spy_sector_weights": {"Technology": 1.5}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_sectors"))


def test_earnings_fixture_validates_and_reader_extracts_date():
    schema = _schema("metron_earnings")
    art = {"schema_version": 1, "as_of": "2026-06-26", "earnings": {"AAPL": "2026-07-30"}}
    _validate(art, schema)
    s3 = type("S3", (), {"get_object": staticmethod(
        lambda Bucket, Key: {"Body": type("B", (), {"read": lambda self: json.dumps(art).encode()})()}
    )})()
    dates = calendar_spine.spine_earnings_dates(["AAPL"], s3=s3)
    from datetime import date
    assert dates["AAPL"] == date(2026, 7, 30)


# ── macro ─────────────────────────────────────────────────────────────────────

def test_macro_fixture_validates_and_reader_extracts_events():
    schema = _schema("metron_macro")
    art = {"schema_version": 2, "as_of": "2026-06-11",
           "series": {"FEDFUNDS": [["2026-06-01", 5.33]]},
           "next_release": {"FEDFUNDS": "2026-07-30"},
           "release_events": [{"date": "2026-07-30", "kind": "fomc", "series_id": "FEDFUNDS",
                                "label": "FOMC decision"}]}
    _validate(art, schema)
    s3 = type("S3", (), {"get_object": staticmethod(
        lambda Bucket, Key: {"Body": type("B", (), {"read": lambda self: json.dumps(art).encode()})()}
    )})()
    events = macro_spine.spine_macro_events(s3=s3)
    assert events and events[0]["series_id"] == "FEDFUNDS"


def test_macro_bad_series_point_fails_schema():
    art = {"schema_version": 2, "as_of": "2026-06-11",
           "series": {"FEDFUNDS": [["2026-06-01"]]}, "next_release": {}, "release_events": []}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_macro"))


# ── fundamentals / analyst / sentiment / valuation_medians / security_performance ──

def test_fundamentals_fixture_validates_and_reader_parses():
    schema = _schema("metron_fundamentals")
    art = {"schema_version": 5, "as_of": "2026-06-12", "source": "yfinance",
           "fundamentals": {"AAPL": {"trailingPE": 30.0, "sector": "Technology"}}}
    _validate(art, schema)
    snap = fundamentals_svc.load_fundamentals(reader=lambda: art)
    assert snap.by_symbol["AAPL"].trailing_pe == pytest.approx(30.0)


def test_fundamentals_wrong_type_fails_schema():
    art = {"schema_version": 5, "as_of": "2026-06-12", "source": "yfinance",
           "fundamentals": {"AAPL": {"trailingPE": "thirty"}}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_fundamentals"))


def test_analyst_fixture_validates_and_reader_parses():
    schema = _schema("metron_analyst")
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "yfinance+finnhub",
           "analyst": {"AAPL": {"consensus_rating": "buy", "num_analysts": 30}}}
    _validate(art, schema)
    snap = analyst_svc.load_analyst(reader=lambda: art)
    assert snap.by_symbol["AAPL"].consensus_rating == "buy"


def test_sentiment_fixture_validates_and_reader_parses():
    schema = _schema("metron_sentiment")
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "news_aggregates_daily(LM)",
           "sentiment": {"AAPL": {"sentiment": 0.2, "n_articles": 5, "as_of": "2026-06-26"}}}
    _validate(art, schema)
    snap = sentiment_svc.load_sentiment(reader=lambda: art)
    assert snap.by_symbol["AAPL"].sentiment == pytest.approx(0.2)


def test_sentiment_out_of_range_fails_schema():
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "news_aggregates_daily(LM)",
           "sentiment": {"AAPL": {"sentiment": 4.0}}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_sentiment"))


def test_valuation_medians_fixture_validates_and_reader_parses():
    schema = _schema("metron_valuation_medians")
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "yfinance",
           "by_sector": {"Technology": {"n": 3, "trailing_pe": 32.0}},
           "by_country": {"United States": {"n": 3, "trailing_pe": 32.0}}}
    _validate(art, schema)
    snap = valuation_medians_svc.load_valuation_medians(reader=lambda: art)
    assert snap.by_sector["Technology"].trailing_pe == pytest.approx(32.0)


def test_valuation_medians_missing_n_fails_schema():
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "yfinance",
           "by_sector": {"Technology": {"trailing_pe": 32.0}}, "by_country": {}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_valuation_medians"))


def test_security_performance_fixture_validates_and_reader_parses():
    schema = _schema("metron_security_performance")
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "computed",
           "performance": {"AAPL": {
               "period_returns": {"1Y": 0.2}, "ytd_pct": 0.1, "ltm_pct": 0.2,
               "volatility": 0.18, "sharpe": 1.2, "sortino": 1.5, "max_drawdown": -0.1,
               "beta_vs_spy": 1.1, "vs_spy_window": 0.03, "vs_spy_1y": 0.05,
               "n_bars": 260, "history_from": "2025-06-26",
           }}}
    _validate(art, schema)
    snap = security_performance_svc.load_security_performance(reader=lambda: art)
    assert snap.by_symbol["AAPL"].n_bars == 260


def test_security_performance_missing_required_field_fails_schema():
    art = {"schema_version": 1, "as_of": "2026-06-26", "source": "computed",
           "performance": {"AAPL": {"period_returns": {}, "ytd_pct": 0.1, "ltm_pct": 0.2}}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_security_performance"))


# ── intraday latest.json ─────────────────────────────────────────────────────

def test_intraday_latest_fixture_validates_and_reader_extracts_quotes():
    schema = _schema("metron_intraday_latest")
    art = {"schema_version": 3, "as_of_utc": "2026-06-12T15:00:00Z", "source": "yfinance_delayed",
           "quotes": {"AAPL": {"last": 200.0, "open": 199.0, "prev_close": 199.5,
                                "session_date": "2026-06-12", "prev_session_date": "2026-06-11",
                                "currency": "USD"}},
           "indices": {}, "fund_proxies": {}}
    _validate(art, schema)
    quotes, as_of, stale = intraday_svc.load_quotes(reader=lambda: art)
    assert quotes["AAPL"]["last"] == 200.0
    assert as_of == "2026-06-12T15:00:00Z"


def test_intraday_latest_missing_currency_fails_schema():
    art = {"schema_version": 3, "as_of_utc": "2026-06-12T15:00:00Z", "source": "yfinance_delayed",
           "quotes": {"AAPL": {"last": 200.0, "open": 199.0, "prev_close": 199.5,
                                "session_date": "2026-06-12", "prev_session_date": "2026-06-11"}},
           "indices": {}, "fund_proxies": {}}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art, _schema("metron_intraday_latest"))
