"""Technical rating feed-gate on the Holdings endpoint (metron-ops#294 closes-when): the
owner (feed-entitled) build shows the rating fields; the no-feed beta build shows none —
exactly like every other yfinance-derived Holdings metric (rsi_14, consensus_rating, …).
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

import pytest

from api.config import settings
from api.services import metrics_enrichment, technical_rating

CSV = "date,type,symbol,quantity,price,amount,account\n2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"


def _rating_art() -> dict:
    # ``load_technical_rating`` (no ``now=`` override, called via enrich_metrics exactly
    # as production does) judges freshness against the REAL wall clock — a hardcoded
    # as_of_utc would silently go stale (bugclass_a_test_fixture_that_expires_at_utc_
    # midnight): anchor to "now" instead.
    return {
        "schema_version": 1,
        "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ratings": {
            "AAPL": {"score": 0.6, "label": "Buy", "ma_score": 0.7, "osc_score": 0.5,
                      "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11},
        },
    }


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def test_owner_build_shows_technical_rating(client, tenant, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    # Capture the real loader BEFORE patching — metrics_enrichment.technical_rating_service
    # IS the technical_rating module (same object, imported under an alias), so patching
    # its `load_technical_rating` attribute would otherwise shadow the very function this
    # lambda calls into (self-reference), same gotcha test_holdings_metrics.py notes.
    real_load = technical_rating.load_technical_rating
    monkeypatch.setattr(
        metrics_enrichment.technical_rating_service, "load_technical_rating",
        lambda: real_load(intraday_reader=_rating_art),
    )
    pid = _seed(client, tenant)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["tech_rating_score"] == 0.6
    assert aapl["tech_rating_label"] == "Buy"
    assert aapl["tech_rating_basis"] == "intraday"
    assert aapl["tech_rating_n_votes"] == 11


def test_beta_no_feed_build_shows_no_technical_rating(client, tenant, monkeypatch):
    """The no-feed beta never even calls the rating reader — ``enrich_metrics`` (which the
    rating is wired into) is gated behind ``settings.feed_entitled`` entirely, same as
    rsi_14 / consensus_rating."""
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", False)

    def _boom():
        raise AssertionError("technical_rating reader must not be called off a feed-entitled build")

    monkeypatch.setattr(metrics_enrichment.technical_rating_service, "load_technical_rating", _boom)
    pid = _seed(client, tenant)
    rows = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    aapl = next(r for r in rows if r["ticker"] == "AAPL")
    assert aapl["tech_rating_score"] is None
    assert aapl["tech_rating_label"] is None
    assert aapl["tech_rating_basis"] is None
    assert aapl["tech_rating_as_of"] is None
    assert aapl["tech_rating_ma_score"] is None
    assert aapl["tech_rating_n_votes"] is None
    # Same beta-gate invariant applies to every other spine-derived Holdings metric.
    assert aapl["rsi_14"] is None
    assert aapl["consensus_rating"] is None
