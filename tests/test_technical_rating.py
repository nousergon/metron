"""Technical rating consumer (metron-ops#294) — the reader against both producer artifacts
(``market_data/technical_ratings/latest.json`` v1 intraday, ``market_data/technicals/latest
.json`` v3 embedded EOD fallback), the Holdings/tearsheet wiring, and the feed-gate. Pure
unit tests (injected readers — no S3, no network); the artifact does not exist in S3 yet
(concurrent sibling producer PR), which is exactly the "absent artifact" path these tests
pin.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from api.services import analytics, metrics_enrichment, technical_rating

SCHEMA_PATH = Path(__file__).parent / "contracts" / "technical_ratings.schema.json"

_NOW = datetime(2026, 9, 14, 15, 0, 0, tzinfo=UTC)  # mid-session ET


def _fresh_intraday_art(as_of: str = "2026-09-14T14:55:00Z") -> dict:
    return {
        "schema_version": 1,
        "as_of_utc": as_of,
        "quote_as_of_utc": as_of,
        "source": "computed_intraday",
        "ratings": {
            "AAPL": {
                "score": 0.6, "label": "Buy", "ma_score": 0.7, "osc_score": 0.5,
                "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11,
                "price": 227.5, "bar_date": "2026-09-14", "basis": "intraday",
            },
        },
    }


_EOD_TECHNICALS_ART = {
    "as_of": "2026-09-13",
    "technicals": {
        "AAPL": {
            "rsi_14": 61.2,
            "rating": {
                "score": -0.2, "label": "Sell", "ma_score": -0.1, "osc_score": -0.3,
                "n_buy": 2, "n_neutral": 3, "n_sell": 6, "n_votes": 11,
            },
        },
        "MSFT": {"rsi_14": 55.0},  # schema v2 shape — no embedded rating, tolerated
    },
}


# ── 0. the pinned producer schema is itself valid, and our fixture conforms ────────────


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def test_pinned_schema_is_valid_and_v1(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"] == 1


def test_fresh_intraday_fixture_conforms_to_pinned_schema(schema):
    jsonschema.validate(instance=_fresh_intraday_art(), schema=schema)


# ── 1. intraday path (fresh) ────────────────────────────────────────────────────────────


def test_fresh_intraday_rating_used():
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: _fresh_intraday_art(),
        technicals_reader=lambda: _EOD_TECHNICALS_ART,
        now=_NOW,
    )
    r = snap.by_symbol["AAPL"]
    assert r.score == 0.6 and r.label == "Buy" and r.basis == "intraday"
    assert r.ma_score == 0.7 and r.osc_score == 0.5
    assert (r.n_buy, r.n_neutral, r.n_sell, r.n_votes) == (8, 2, 1, 11)
    assert r.as_of == "2026-09-14T14:55:00Z"


def test_stale_intraday_falls_back_to_eod():
    # as_of_utc 25 minutes before "now" — past intraday.STALE_AFTER_SECONDS (20 min).
    stale_art = _fresh_intraday_art(as_of="2026-09-14T14:35:00Z")
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: stale_art,
        technicals_reader=lambda: _EOD_TECHNICALS_ART,
        now=_NOW,
    )
    r = snap.by_symbol["AAPL"]
    assert r.basis == "eod" and r.label == "Sell" and r.score == -0.2
    assert r.as_of == "2026-09-13"


def test_missing_intraday_artifact_falls_back_to_eod():
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: None,
        technicals_reader=lambda: _EOD_TECHNICALS_ART,
        now=_NOW,
    )
    r = snap.by_symbol["AAPL"]
    assert r.basis == "eod" and r.score == -0.2


def test_symbol_absent_from_fresh_intraday_falls_back_to_eod_per_symbol():
    """A fresh intraday artifact that simply doesn't carry a symbol still lets that symbol
    resolve from the EOD fallback — the freshness decision is per-symbol, not all-or-nothing."""
    art = _fresh_intraday_art()
    art["ratings"] = {}  # fresh artifact, but no ratings for anyone yet
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: art,
        technicals_reader=lambda: _EOD_TECHNICALS_ART,
        now=_NOW,
    )
    r = snap.by_symbol["AAPL"]
    assert r.basis == "eod"


# ── 2. EOD fallback / v2-vs-v3 tolerance ────────────────────────────────────────────────


def test_schema_v2_technicals_no_rating_yields_no_entry():
    """MSFT has no embedded ``rating`` (schema v2 shape) — omitted, never fabricated."""
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: None, technicals_reader=lambda: _EOD_TECHNICALS_ART, now=_NOW,
    )
    assert "MSFT" not in snap.by_symbol


def test_both_artifacts_absent_is_empty():
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: None, technicals_reader=lambda: None, now=_NOW,
    )
    assert snap.by_symbol == {}


def test_unknown_label_is_omitted_never_fabricated():
    art = {
        "schema_version": 1, "as_of_utc": "2026-09-14T14:55:00Z",
        "ratings": {"ZZZ": {"score": 0.4, "label": "Bullish!", "n_votes": 5}},
    }
    snap = technical_rating.load_technical_rating(
        intraday_reader=lambda: art, technicals_reader=lambda: None, now=_NOW,
    )
    r = snap.by_symbol["ZZZ"]
    assert r.score == 0.4 and r.label is None


# ── 3. Holdings enrichment wiring ───────────────────────────────────────────────────────


def test_enrich_metrics_maps_technical_rating(monkeypatch):
    held = [analytics.Holding(ticker="AAPL", quantity=1, avg_cost=1, cost_basis=1)]
    monkeypatch.setattr(metrics_enrichment.tearsheet_service, "_yf_symbol_map",
                        lambda session, syms: {"AAPL": "AAPL"})
    monkeypatch.setattr(metrics_enrichment.fundamentals_service, "load_fundamentals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment",
                        lambda: type("S", (), {"by_symbol": {}})())
    # metrics_enrichment.technical_rating_service IS the technical_rating module (same
    # object, imported under an alias) — capture the real loader BEFORE patching, or the
    # lambda below would call into itself (see test_holdings_metrics.py's identical note).
    real_load = technical_rating.load_technical_rating
    monkeypatch.setattr(
        metrics_enrichment.technical_rating_service, "load_technical_rating",
        lambda: real_load(
            intraday_reader=lambda: _fresh_intraday_art(),
            technicals_reader=lambda: _EOD_TECHNICALS_ART,
            now=_NOW,
        ),
    )

    metrics_enrichment.enrich_metrics(session=None, held=held)
    aapl = held[0]
    assert aapl.tech_rating_score == 0.6 and aapl.tech_rating_label == "Buy"
    assert aapl.tech_rating_basis == "intraday"
    assert aapl.tech_rating_ma_score == 0.7 and aapl.tech_rating_osc_score == 0.5
    assert aapl.tech_rating_n_votes == 11


def test_enrich_metrics_no_rating_leaves_fields_none(monkeypatch):
    held = [analytics.Holding(ticker="ZZZ", quantity=1, avg_cost=1, cost_basis=1)]
    monkeypatch.setattr(metrics_enrichment.tearsheet_service, "_yf_symbol_map",
                        lambda session, syms: {"ZZZ": "ZZZ"})
    monkeypatch.setattr(metrics_enrichment.fundamentals_service, "load_fundamentals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.technical_rating_service, "load_technical_rating",
                        lambda: technical_rating.RatingSnapshot(by_symbol={}))

    metrics_enrichment.enrich_metrics(session=None, held=held)
    zzz = held[0]
    assert zzz.tech_rating_score is None and zzz.tech_rating_label is None
    assert zzz.tech_rating_basis is None
