"""API responses that serve market-reader values carry the producer object they came from
(``input_source``), additively (metron-ops-I373)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

from api.config import settings
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


# ── HTTP-level: valuation medians, holdings enrichment, watchlist, market board ──────────────

_CSV = "date,type,symbol,quantity,price,amount,account\n2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"


def _seed_http(client):
    hdr = {"X-Tenant-Id": str(uuid.uuid4())}
    pid = client.post("/portfolios", json={"name": "P"}, headers=hdr).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(_CSV.encode()), "text/csv")},
        headers=hdr,
    )
    assert r.status_code == 200
    return pid, hdr


def _ratings_art() -> dict:
    return {
        "schema_version": 2,
        "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ratings": {"AAPL": {"score": 0.6, "label": "Buy", "ma_score": 0.7, "osc_score": 0.5,
                             "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11}},
    }


def _patch_readers(monkeypatch):
    refused = {"schema_version": 999}  # a refused artifact still names the object it refused
    monkeypatch.setattr(fundamentals, "_default_read", lambda: (refused, _src("fund")))
    monkeypatch.setattr(technicals, "_default_read", lambda: (refused, _src("tech")))
    monkeypatch.setattr(analyst, "_default_read", lambda: (refused, _src("analyst")))
    monkeypatch.setattr(sentiment, "_default_read", lambda: (refused, _src("sent")))
    monkeypatch.setattr(technical_rating, "_default_intraday_read", lambda: (_ratings_art(), _src("ratings")))
    monkeypatch.setattr(technical_rating, "_default_technicals_read", lambda: (None, _src("tech-eod")))


def test_valuation_medians_endpoint_serves_input_source(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    monkeypatch.setattr(
        valuation_medians, "_default_read",
        lambda: ({"schema_version": 1, "as_of": "2026-11-13", "by_sector": {}, "by_country": {}}, _src("medians")),
    )
    pid, hdr = _seed_http(client)
    body = client.get(f"/portfolios/{pid}/valuation-medians", headers=hdr).json()
    assert body["input_source"] == {
        "artifact": "s3://bkt/medians.json",
        "version_id": "v-medians",
        "etag": "e-medians",
        "adjustment_basis": None,
        "ref": "s3://bkt/medians.json?versionId=v-medians",
    }


def test_valuation_medians_endpoint_without_lineage_is_null(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    monkeypatch.setattr(valuation_medians, "_default_read", lambda: ({"schema_version": 1}, None))
    pid, hdr = _seed_http(client)
    assert client.get(f"/portfolios/{pid}/valuation-medians", headers=hdr).json()["input_source"] is None


_EXPECTED_METRIC_SOURCES = {
    "fundamentals": "s3://bkt/fund.json?versionId=v-fund",
    "technicals": "s3://bkt/tech.json?versionId=v-tech",
    "analyst": "s3://bkt/analyst.json?versionId=v-analyst",
    "sentiment": "s3://bkt/sent.json?versionId=v-sent",
    f"technical_rating:{technical_rating.TECHNICAL_RATINGS_KEY}": "s3://bkt/ratings.json?versionId=v-ratings",
    f"technical_rating:{technical_rating.TECHNICALS_KEY}": "s3://bkt/tech-eod.json?versionId=v-tech-eod",
}


def test_holdings_endpoint_serves_input_sources(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch_readers(monkeypatch)
    pid, hdr = _seed_http(client)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=hdr).json()
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert {k: v["ref"] for k, v in aapl["input_sources"].items()} == _EXPECTED_METRIC_SOURCES
    assert aapl["input_sources"]["fundamentals"]["version_id"] == "v-fund"
    assert aapl["tech_rating_score"] == 0.6  # the served value is unchanged by the lineage


def test_holdings_endpoint_off_feed_has_no_input_sources(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)
    pid, hdr = _seed_http(client)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=hdr).json()
    assert rows and all(r["input_sources"] == {} for r in rows)


def test_watchlist_endpoint_serves_input_sources(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch_readers(monkeypatch)
    pid, hdr = _seed_http(client)
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "NVDA"}, headers=hdr)
    rows = client.get(f"/portfolios/{pid}/watchlist", headers=hdr).json()
    nvda = next(r for r in rows if r["symbol"] == "NVDA")
    assert {k: v["ref"] for k, v in nvda["input_sources"].items()} == _EXPECTED_METRIC_SOURCES


def test_market_board_endpoint_serves_input_sources(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    _patch_readers(monkeypatch)
    pid, hdr = _seed_http(client)
    body = client.get(f"/portfolios/{pid}/market-board", params={"scope": "held"}, headers=hdr).json()
    assert {k: v["ref"] for k, v in body["input_sources"].items()} == {
        f"technical_rating:{technical_rating.TECHNICAL_RATINGS_KEY}": "s3://bkt/ratings.json?versionId=v-ratings",
        f"technical_rating:{technical_rating.TECHNICALS_KEY}": "s3://bkt/tech-eod.json?versionId=v-tech-eod",
    }
    assert [r["symbol"] for r in body["rows"]] == ["AAPL"]
