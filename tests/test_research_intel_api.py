"""GET /research-intel — entitlement gate + last-good/stale behavior (config#1499 Phase 1)."""

from __future__ import annotations

import pytest

from api.config import settings
from api.routers import research_intel as router_mod
from portfolio_analytics.ingestion.research_intel_connector import ResearchIntelSnapshot


def _snapshot() -> ResearchIntelSnapshot:
    return ResearchIntelSnapshot.from_artifact(
        {
            "schema_version": 1,
            "date": "2026-07-04",
            "market_regime": "bull",
            "regime_narrative": "Risk-on.",
            "sector_ratings": {"Technology": {"rating": "overweight"}},
            "sector_modifiers": {"Technology": 1.1},
            "market_breadth": {"pct_above_50d_ma": 60.0},
            "attractiveness": {
                "AAPL": {"ticker": "AAPL", "score": 80.0, "sector": "Technology"},
                "XOM": {"ticker": "XOM", "score": 40.0, "sector": "Energy"},
            },
        }
    )


@pytest.fixture()
def _cached(monkeypatch):
    """Serve a populated last-good snapshot without touching the filesystem/S3."""
    monkeypatch.setattr(router_mod, "load_research_intel", lambda: _snapshot())


def test_paid_tier_returns_intel(client, monkeypatch, _cached):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "personal")
    r = client.get("/research-intel")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["stale"] is False
    assert body["intel"]["market_regime"] == "bull"
    assert set(body["intel"]["attractiveness"]) == {"AAPL", "XOM"}
    # The internal fetch-error field is never surfaced on the read path.
    assert "error" not in body["intel"]


def test_tickers_filter_scopes_attractiveness(client, monkeypatch, _cached):
    monkeypatch.setattr(settings, "default_tier", "personal")
    body = client.get("/research-intel", params={"tickers": "aapl"}).json()
    assert set(body["intel"]["attractiveness"]) == {"AAPL"}
    # Global regime/sector context is unaffected by the ticker scope.
    assert body["intel"]["market_regime"] == "bull"
    assert body["intel"]["sector_ratings"]["Technology"]["rating"] == "overweight"


def test_beta_tier_is_gated_no_intel_leaked(client, monkeypatch, _cached):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "beta")
    body = client.get("/research-intel").json()
    assert body["available"] is False
    assert body["reason"] == "tier"
    assert body["required_tier"] == "personal"
    assert body["intel"] is None


def test_paid_tier_empty_cache_is_stale_not_error(client, monkeypatch):
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(router_mod, "load_research_intel", lambda: None)
    body = client.get("/research-intel").json()
    assert body["available"] is True
    assert body["stale"] is True
    assert body["intel"] is None


def test_simulator_preview_downgrades_to_beta(client, monkeypatch, _cached):
    """With the owner simulator on, X-Preview-Tier=beta gates the paid default down."""
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    body = client.get("/research-intel", headers={"X-Preview-Tier": "beta"}).json()
    assert body["available"] is False
    assert body["reason"] == "tier"
