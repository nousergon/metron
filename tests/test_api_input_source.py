"""API responses that serve market-reader values carry the producer object they came from
(``input_source``), additively (metron-ops-I373)."""

from __future__ import annotations

from api.routers.portfolios import TearsheetOut
from api.services import (
    analyst,
    fundamentals,
    indices,
    security_performance,
    sentiment,
    tearsheet,
    technical_rating,
    technicals,
    valuation_medians,
)
from portfolio_analytics.prices.source import InputSource
from tests.test_indices import _ART
from tests.test_tearsheet import _PERF_ART, _TECH_ART, _seed


def _src(name: str) -> InputSource:
    return InputSource(artifact=f"s3://bkt/{name}.json", version_id=f"v-{name}", etag=f"e-{name}")


def test_indices_endpoint_serves_input_source(client, monkeypatch):
    monkeypatch.setattr(indices, "_default_read", lambda: (_ART, _src("intraday")))
    monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
    body = client.get("/indices/intraday").json()
    assert body["input_source"] == {
        "artifact": "s3://bkt/intraday.json",
        "version_id": "v-intraday",
        "etag": "e-intraday",
        "adjustment_basis": None,
        "ref": "s3://bkt/intraday.json?versionId=v-intraday",
    }


def test_indices_endpoint_input_source_is_null_without_lineage(client, monkeypatch):
    monkeypatch.setattr(indices, "_default_read", lambda: (_ART, None))
    monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
    assert client.get("/indices/intraday").json()["input_source"] is None


def test_valuation_medians_snapshot_input_source_feeds_the_response(client, monkeypatch):
    monkeypatch.setattr(valuation_medians, "_default_read", lambda: ({"schema_version": 1}, _src("medians")))
    snap = valuation_medians.load_valuation_medians()
    assert snap.input_source == _src("medians")


def test_tearsheet_response_names_every_reader_object(db_session, monkeypatch):
    refused = {"schema_version": 999}  # a refused artifact still names the object it refused
    monkeypatch.setattr(security_performance, "_default_read", lambda: (_PERF_ART, _src("perf")))
    monkeypatch.setattr(technicals, "_default_read", lambda: (_TECH_ART, _src("tech")))
    monkeypatch.setattr(technical_rating, "_default_intraday_read", lambda: (None, _src("ratings")))
    monkeypatch.setattr(technical_rating, "_default_technicals_read", lambda: (_TECH_ART, _src("tech-eod")))
    monkeypatch.setattr(fundamentals, "_default_read", lambda: (refused, _src("fund")))
    monkeypatch.setattr(analyst, "_default_read", lambda: (refused, _src("analyst")))
    monkeypatch.setattr(sentiment, "_default_read", lambda: (refused, _src("sent")))
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=True)
    assert sheet is not None
    assert sheet.input_sources == {
        "security_performance": _src("perf"),
        "technicals": _src("tech"),
        f"technical_rating:{technical_rating.TECHNICAL_RATINGS_KEY}": _src("ratings"),
        f"technical_rating:{technical_rating.TECHNICALS_KEY}": _src("tech-eod"),
        "fundamentals": _src("fund"),
        "analyst": _src("analyst"),
        "sentiment": _src("sent"),
    }
    # The response model serialises the dataclass lineage as additive JSON.
    out = TearsheetOut.model_validate(sheet).model_dump(mode="json")
    assert out["input_sources"]["technicals"]["ref"] == "s3://bkt/tech.json?versionId=v-tech"
    assert set(out["input_sources"]) == {
        "security_performance", "technicals", "fundamentals", "analyst", "sentiment",
        f"technical_rating:{technical_rating.TECHNICAL_RATINGS_KEY}",
        f"technical_rating:{technical_rating.TECHNICALS_KEY}",
    }


def test_tearsheet_off_feed_has_no_input_sources(db_session):
    tenant_id, pid = _seed(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=False)
    assert sheet is not None and sheet.input_sources == {}
