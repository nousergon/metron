"""Contract tests for the insight facet catalog (metron-ops#249).

Two of these assertions are compliance controls rather than hygiene — the E8 and H5 level
assignments — and they carry the reasoning inline so a future edit that flips one has to
argue with the test rather than quietly pass it.
"""

from __future__ import annotations

import pytest

from api import entitlements
from api.insights import registry
from api.insights.registry import (
    CATALOG,
    FACET_BY_KEY,
    FAMILIES,
    LEVEL_L1,
    LEVEL_L2,
    candidate_facets,
    facets_in_family,
)


def test_catalog_is_non_empty_and_keys_unique():
    assert len(CATALOG) > 50, "the catalog is the candidate pool — a thin one starves the ranker"
    keys = [f.key for f in CATALOG]
    assert len(keys) == len(set(keys)), "duplicate facet keys would collide in FACET_BY_KEY"
    assert set(FACET_BY_KEY) == set(keys)


def test_every_family_is_declared_and_populated():
    """A family in FAMILIES with no facets is a doc-vs-code drift; the reverse is a typo."""
    used = {f.family for f in CATALOG}
    assert used == set(FAMILIES)
    for family in FAMILIES:
        assert facets_in_family(family), f"family {family!r} is declared but empty"


def test_every_facet_names_real_entitlement_sources_and_features():
    """entitlements stays the single source of truth — the registry references, never restates."""
    for facet in CATALOG:
        assert facet.feature in entitlements.FEATURE_BY_KEY, f"{facet.key}: unknown feature {facet.feature!r}"
        for source in facet.requires:
            assert source in entitlements.ALL_SOURCES, f"{facet.key}: unknown source {source!r}"
        assert facet.requires, f"{facet.key}: every facet needs at least one data source"


def test_every_facet_declares_a_known_level_and_a_surface():
    for facet in CATALOG:
        assert facet.level in registry.LEVELS, f"{facet.key}: bad level {facet.level!r}"
        assert facet.surface, f"{facet.key}: no tap-through surface — 'one tap, never two' fails without it"
        assert facet.label.strip(), f"{facet.key}: no label"


def test_catalog_is_predominantly_l1():
    """L1 must be a complete product, not a locked preview — so most facets ship pre-registration."""
    l1 = [f for f in CATALOG if f.level == LEVEL_L1]
    assert len(l1) / len(CATALOG) > 0.9


# ── The two compliance controls ──────────────────────────────────────────────


def test_attractiveness_is_l1_and_carries_the_rate_the_security_warning():
    """E8: a universal rating is legal as impersonal market intelligence.

    It stays L1 — but the note is load-bearing, because the same rating rendered next to
    YOUR position, sorted by YOUR weight, with a decile flag, is where the publisher
    exemption gets tested. If this note ever disappears the guidance disappears with it.
    """
    facet = FACET_BY_KEY["security_attractiveness"]
    assert facet.level == LEVEL_L1
    assert "never the holding" in facet.notes


def test_facets_where_metron_picks_the_names_are_l2():
    """H5 + E9 + F8: the three places a directive can enter by accident.

    Surfacing unheld names Metron chose is a recommendation regardless of hedging; the
    L1-legal forms are the ones where the USER authored the filter. Naming which losses
    to harvest, and emitting a buy/sell signal on a held name, are directives outright.
    """
    for key in ("unheld_rated_names", "held_buy_sell_signal", "recommended_harvest_set"):
        assert FACET_BY_KEY[key].level == LEVEL_L2, f"{key} must be L2 — Metron picks, so it is a directive"


def test_user_authored_rule_facets_are_l1():
    """The personalization trick: evaluating a rule the user typed is arithmetic, not advice."""
    for key in ("target_allocation_drift", "position_cap_breach", "screener_entries", "target_exposure_gap"):
        assert FACET_BY_KEY[key].level == LEVEL_L1


# ── Candidate filtering ──────────────────────────────────────────────────────


def test_candidate_facets_excludes_l2_by_default():
    """A pre-registration surface is safe by construction, not by the caller remembering."""
    candidates = candidate_facets("personal", feed_enabled=True)
    assert candidates, "the richest tier with the feed on should yield candidates"
    assert all(f.level == LEVEL_L1 for f in candidates)


def test_candidate_facets_can_include_l2_when_asked():
    l1_only = candidate_facets("personal", feed_enabled=True)
    with_l2 = candidate_facets("personal", feed_enabled=True, max_level=LEVEL_L2)
    assert len(with_l2) > len(l1_only)
    assert any(f.level == LEVEL_L2 for f in with_l2)


def test_candidate_facets_rejects_an_unknown_level():
    with pytest.raises(ValueError, match="unknown level"):
        candidate_facets("beta", feed_enabled=False, max_level="L3")


def test_beta_tier_without_feed_excludes_feed_gated_facets():
    """Filtering happens BEFORE ranking — an uncomputable facet never enters the pool."""
    candidates = candidate_facets("beta", feed_enabled=False)
    assert candidates, "the beta tier still has plenty to say from broker + ledger data"
    provisioned = entitlements.provisioned_sources(False)
    for facet in candidates:
        assert all(r in provisioned for r in facet.requires)
        assert "feed" not in facet.requires


def test_feed_toggle_widens_the_beta_pool_only_within_tier():
    """The two axes are independent: flipping the feed cannot smuggle in a tier-gated facet."""
    no_feed = candidate_facets("beta", feed_enabled=False)
    with_feed = candidate_facets("beta", feed_enabled=True)
    assert len(with_feed) >= len(no_feed)
    beta_features = entitlements.TIER_BY_KEY["beta"].features
    for facet in with_feed:
        assert facet.feature in beta_features


def test_richer_tier_yields_at_least_as_many_candidates():
    beta = candidate_facets("beta", feed_enabled=True)
    personal = candidate_facets("personal", feed_enabled=True)
    assert len(personal) > len(beta)
    assert {f.key for f in beta} <= {f.key for f in personal}


def test_disabled_facets_never_reach_a_surface():
    """A registered-but-unbuilt facet is honest in the catalog and absent from the pool."""
    disabled = [f for f in CATALOG if not f.enabled]
    assert disabled, "insight_outcome_history is registered disabled pending the insight ledger"
    candidate_keys = {f.key for f in candidate_facets("personal", feed_enabled=True, max_level=LEVEL_L2)}
    for facet in disabled:
        assert facet.key not in candidate_keys


def test_facets_in_family_preserves_catalog_order():
    structure = facets_in_family("structure")
    order = [f.key for f in CATALOG if f.family == "structure"]
    assert [f.key for f in structure] == order


def test_facets_in_family_is_empty_for_an_unknown_family():
    assert facets_in_family("not-a-family") == ()
