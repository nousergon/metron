"""Entitlement model + /meta/entitlements endpoint (the tier-simulator source of truth).

Pins the two-axis gating: a feature is AVAILABLE only when its tier includes it AND
its data sources are provisioned (the feed toggle flips the licensed sources). And
the simulator override is honored ONLY when ``tier_simulator`` is on.
"""

from __future__ import annotations

import pytest

from api import entitlements as ent
from api.config import settings

# ── model: catalog integrity ─────────────────────────────────────────────────

def test_catalog_validates_at_import():
    # _validate() ran at import; re-run to assert it stays clean.
    ent._validate()


def test_tiers_nest_cheapest_to_richest():
    keys = [t.features for t in ent.TIERS]
    for prev, cur in zip(keys, keys[1:], strict=False):
        assert prev <= cur


def test_required_tier():
    assert ent.required_tier("overview") == "beta"
    # Only two exposed tiers now → everything the beta tier excludes upsells to "personal"
    # (the full AI-Advisor build), never the internal Pro/Research layers.
    assert ent.required_tier("risk") == "personal"
    assert ent.required_tier("agentic_research") == "personal"
    assert ent.required_tier("ai_advisor") == "personal"
    assert ent.required_tier("nonexistent") is None


# ── model: resolve() ─────────────────────────────────────────────────────────

def _by_key(result):
    return {f["key"]: f for f in result["features"]}


def test_beta_no_feed_is_the_free_derivable_set():
    feats = _by_key(ent.resolve("beta", feed_enabled=False))
    # The free-derivable beta set is available with NO feed.
    for k in ("overview", "income", "transactions", "tax", "concentration",
              "performance", "macro", "fundamentals"):
        assert feats[k]["available"], k
    # The wedge isn't in the beta tier at all → reason "tier" (upsell to the full tier).
    for k in ("risk", "attribution", "scenarios", "benchmark", "etf_lookthrough"):
        assert not feats[k]["available"]
        assert feats[k]["reason"] == "tier"
        assert feats[k]["required_tier"] == "personal"
    assert not feats["ai_advisor"]["available"]
    assert feats["ai_advisor"]["reason"] == "tier"
    # research_intel (paid AI-Advisor edge output) is not in the beta tier either.
    assert not feats["research_intel"]["available"]
    assert feats["research_intel"]["reason"] == "tier"
    assert feats["research_intel"]["required_tier"] == "personal"


def test_full_tier_without_feed_blocks_the_wedge_on_data_not_tier():
    """The full (AI-Advisor) tier INCLUDES the wedge, but with the feed off it isn't
    computable — the exact thing the feed toggle is meant to show."""
    feats = _by_key(ent.resolve("personal", feed_enabled=False))
    for k in ("risk", "attribution", "scenarios"):
        assert feats[k]["in_tier"] and not feats[k]["computable"]
        assert not feats[k]["available"]
        assert feats[k]["reason"] == "feed"
    assert feats["benchmark"]["reason"] == "benchmark"
    assert feats["etf_lookthrough"]["reason"] == "etf_vendor"
    # Free-derivable features still available without a feed.
    assert feats["performance"]["available"]
    assert feats["macro"]["available"]
    # Advice overlays that need no data source are available even with no feed
    # (agentic_research still needs the feed, so it stays unavailable).
    assert feats["ai_advisor"]["available"]
    assert feats["alpha_engine"]["available"]
    # research_intel is tier-gated only (no data source) → available even with the feed off.
    assert feats["research_intel"]["available"]
    assert not feats["agentic_research"]["available"]
    assert feats["agentic_research"]["reason"] == "feed"


def test_personal_with_feed_is_everything():
    feats = _by_key(ent.resolve("personal", feed_enabled=True))
    assert all(f["available"] for f in feats.values())


def test_resolve_unknown_tier_raises():
    with pytest.raises(ValueError):
        ent.resolve("enterprise", feed_enabled=True)


# ── endpoint ─────────────────────────────────────────────────────────────────

def test_endpoint_default_ignores_preview_when_simulator_off(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "default_tier", "personal")
    monkeypatch.setattr(settings, "feed_entitled", True)
    r = client.get("/meta/entitlements", params={"preview_tier": "beta", "preview_feed": False})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "personal"      # preview_tier IGNORED (simulator off)
    assert body["feed_enabled"] is True
    assert body["simulator"] is False


def test_endpoint_simulator_honors_preview(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", True)
    monkeypatch.setattr(settings, "default_tier", "personal")
    r = client.get("/meta/entitlements", params={"preview_tier": "beta", "preview_feed": False})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "beta"
    assert body["feed_enabled"] is False
    assert body["simulator"] is True
    feats = {f["key"]: f for f in body["features"]}
    assert feats["risk"]["available"] is False


def test_endpoint_simulator_bad_tier_is_400(client, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", True)
    r = client.get("/meta/entitlements", params={"preview_tier": "bogus"})
    assert r.status_code == 400
