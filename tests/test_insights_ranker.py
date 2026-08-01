"""Tests for the deterministic insight ranker (metron-ops#249).

The floor-behaviour tests are the ones that matter most: a glance screen is where a thin
product becomes obvious, and the failure mode is five boring insights rather than a
missing feature.
"""

from __future__ import annotations

import pytest

from api.insights.ranker import (
    DEFAULT_WEIGHTS,
    FLOOR_TEXT,
    Observation,
    RankWeights,
    rank,
    score,
    select,
    with_text,
)


def obs(key: str, **kw) -> Observation:
    params = {"text": f"something about {key}", "as_of": "2026-07-30T16:00:00Z"}
    params.update(kw)
    return Observation(facet_key=key, **params)


# ── Observation validation ───────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["materiality", "deviation", "urgency"])
@pytest.mark.parametrize("bad", [-0.01, 1.5])
def test_scoring_inputs_must_be_normalised(field, bad):
    """Producers map their own domain onto 0–1; an unnormalised input would silently dominate."""
    with pytest.raises(ValueError, match=field):
        obs("concentration_hhi", **{field: bad})


def test_observation_requires_text():
    with pytest.raises(ValueError, match="empty text"):
        obs("cash_drag", text="   ")


def test_observation_requires_provenance():
    """Nothing reaches a surface unprovenanced — positioning §3g.4 law 4."""
    with pytest.raises(ValueError, match="no as-of provenance"):
        obs("cash_drag", as_of="")


# ── Scoring ──────────────────────────────────────────────────────────────────


def test_score_is_a_weighted_sum_of_its_inputs():
    o = obs("x", materiality=0.5, deviation=0.5, urgency=0.5)
    expected = DEFAULT_WEIGHTS.materiality * 0.5 + DEFAULT_WEIGHTS.deviation * 0.5 + DEFAULT_WEIGHTS.urgency * 0.5
    assert score(o) == pytest.approx(expected)


def test_a_user_authored_rule_breach_outweighs_a_comparable_market_fact():
    """The one input the user explicitly asked to be told about, and it is arithmetic."""
    breach = obs("position_cap_breach", materiality=0.3, rule_breach=True)
    market = obs("sector_exposure_shift", materiality=0.3, deviation=0.5)
    assert score(breach) > score(market)


def test_already_shown_is_penalised_not_excluded():
    """A genuinely material fact may repeat — but it has to out-score a fresh one to do so."""
    o = obs("cash_drag", materiality=0.8)
    assert score(o, already_shown=["cash_drag"]) == pytest.approx(
        score(o) - DEFAULT_WEIGHTS.novelty_penalty
    )
    fresh_weak = obs("fee_drag", materiality=0.5)
    stale_strong = obs("cash_drag", materiality=1.0)
    slate = rank([fresh_weak, stale_strong], already_shown=["cash_drag"])
    assert slate[0].facet_key == "cash_drag"


# ── Ranking ──────────────────────────────────────────────────────────────────


def test_rank_orders_best_first_and_respects_limit():
    slate = rank(
        [obs("a", materiality=0.1), obs("b", materiality=0.9), obs("c", materiality=0.5)],
        limit=2,
    )
    assert [o.facet_key for o in slate] == ["b", "c"]


def test_rank_is_deterministic_regardless_of_candidate_order():
    candidates = [obs("a", materiality=0.4), obs("b", materiality=0.6), obs("c", materiality=0.4)]
    assert rank(candidates) == rank(list(reversed(candidates)))


def test_ties_break_on_facet_key_not_input_order():
    """Stable ordering — a slate must not depend on the order producers happened to run."""
    slate = rank([obs("zulu", materiality=0.5), obs("alpha", materiality=0.5)])
    assert [o.facet_key for o in slate] == ["alpha", "zulu"]


def test_rank_rejects_a_negative_limit():
    with pytest.raises(ValueError, match="negative"):
        rank([obs("a")], limit=-1)


def test_rank_of_nothing_is_nothing():
    assert rank([]) == ()


def test_custom_weights_change_the_order():
    """The extension point: metron_ext supplies a tuned policy without forking this module."""
    urgent = obs("long_term_boundary", urgency=1.0)
    material = obs("concentration_hhi", materiality=0.9)
    assert rank([urgent, material])[0].facet_key == "concentration_hhi"
    urgency_first = RankWeights(materiality=0.1, urgency=5.0)
    assert rank([urgent, material], weights=urgency_first)[0].facet_key == "long_term_boundary"


# ── The floor ────────────────────────────────────────────────────────────────


def test_a_quiet_day_returns_the_floor_rather_than_padding():
    """Filling slots for the sake of fullness is how a daily surface loses trust."""
    trivia = [obs("a", materiality=0.01), obs("b", deviation=0.02)]
    selection = select(trivia)
    assert selection.is_floor
    assert len(selection.observations) == 1
    assert selection.observations[0].text == FLOOR_TEXT


def test_the_floor_statement_is_provenanced_from_the_best_candidate():
    """Even a discarded slate carries an as-of, so the floor is never unprovenanced."""
    selection = select([obs("a", materiality=0.01, as_of="2026-07-30T09:32:00Z")])
    assert selection.is_floor
    assert selection.observations[0].as_of == "2026-07-30T09:32:00Z"


def test_the_floor_accepts_an_explicit_as_of_when_there_are_no_candidates_at_all():
    selection = select([], as_of="2026-07-30T09:32:00Z")
    assert selection.is_floor
    assert selection.observations[0].as_of == "2026-07-30T09:32:00Z"


def test_no_candidates_and_no_as_of_fails_loudly():
    """"Nothing unusual today" is a claim about a moment.

    A caller with no candidates and no as-of cannot name that moment, so it does not know
    what data it is speaking for. Emitting the floor anyway would be the single case where
    a surface says something it cannot stand behind — so it raises instead of degrading.
    """
    with pytest.raises(ValueError, match="without provenance"):
        select([])


def test_a_slate_that_clears_the_floor_is_not_topped_up_with_trivia():
    """One good observation beats one good observation plus four filler ones."""
    selection = select([obs("good", materiality=0.9), obs("trivial", materiality=0.01)])
    assert not selection.is_floor
    assert [o.facet_key for o in selection.observations] == ["good"]


def test_floor_threshold_is_tunable():
    candidates = [obs("a", materiality=0.3)]
    assert not select(candidates, floor_threshold=0.2).is_floor
    assert select(candidates, floor_threshold=0.9).is_floor


# ── The generation seam ──────────────────────────────────────────────────────


def test_generated_phrasing_cannot_alter_its_own_priority():
    """Rank first, phrase second — re-ranking a phrased slate yields the same order."""
    original = obs("twr_mwr_gap", materiality=0.7, deviation=0.4)
    phrased = with_text(original, "Your timing cost 1.8 percentage points this year.")
    assert phrased.text != original.text
    assert score(phrased) == score(original)
    assert rank([phrased]) == (phrased,)
