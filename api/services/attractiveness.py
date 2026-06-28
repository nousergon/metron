"""Composite per-holding **attractiveness score** (metron-ops#106, Phase 2).

A transparent, inspectable 0–100 score that blends the free Phase-1 component inputs already
on the spine — forward-P/E vs the holding's sector/country median, analyst price-target
upside, consensus rating, estimate-revision momentum, and news sentiment — into one
"see attractiveness at a glance" number for the Holdings headline column + the tearsheet gauge.

Design (deliberately NOT a black box):
  * Each component is mapped to a unit sub-score ∈ [0, 1] by an EXPLICIT, documented transform
    (`_score_*` below). 0.5 is neutral; >0.5 is more attractive.
  * Component weights are module-level constants (`WEIGHTS`) — readable and unit-pinned.
  * A component whose input is missing (coverage gap / paid-feed-only) is DROPPED and the
    remaining weights are renormalized, so a partial-coverage holding still gets an honest
    score (never fabricated, never penalized for a feed it can't see). `coverage` reports how
    many of the components actually contributed.
  * The final score is `100 × Σ(wᵢ·sᵢ) / Σ wᵢ` over the present components. None when nothing
    is present (honest "—", never a fake 50).

Pure function of already-loaded values — no S3, no DB, no I/O — so it is trivially testable and
identical between the Holdings list path (`portfolios._enrich_metrics`) and the tearsheet path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Component weights (must sum to 1.0; inspectable + unit-pinned). ───────────────────────────
# Chosen to favor forward-looking, analyst-grounded signals over the noisier news channel:
#   valuation (cheap vs peers) and price-target upside lead, consensus rating + revision
#   momentum reinforce, news sentiment is a light tie-breaker. Edit here to retune — the gauge
#   breakdown surfaces the weights so the blend is always auditable.
WEIGHTS: dict[str, float] = {
    "valuation": 0.30,      # forward-P/E vs the sector/country median (cheaper = better)
    "upside": 0.25,         # mean analyst price target vs the live price
    "rating": 0.20,         # analyst consensus rating (signed score)
    "revision": 0.15,       # estimate-revision momentum (paid feed; dropped until it lands)
    "sentiment": 0.10,      # news sentiment (trust-weighted LM composite)
}

# Saturation bounds for the unbounded inputs — beyond these a component is fully (un)attractive.
# Documented so the transforms are reproducible by hand.
_UPSIDE_CAP = 0.50          # ±50% target upside saturates the upside sub-score
_VALUATION_CAP = 0.50       # ±50% fwd-P/E discount/premium vs median saturates valuation
_REVISION_CAP = 0.20        # ±20% revision trend saturates the revision sub-score


@dataclass
class Component:
    """One inspectable line of the score breakdown (drives the tearsheet gauge tooltip)."""

    key: str
    weight: float           # the (pre-renormalization) catalog weight
    sub_score: float        # unit sub-score ∈ [0, 1]


@dataclass
class Attractiveness:
    """A holding's composite attractiveness + its fully-inspectable component breakdown."""

    score: float                                   # 0–100 headline number
    coverage: int                                  # # of components that contributed
    components: list[Component] = field(default_factory=list)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _score_valuation(fwd_pe: float | None, median_fwd_pe: float | None) -> float | None:
    """Cheaper-than-peers → more attractive. Discount = 1 − fwd_pe/median; a `_VALUATION_CAP`
    discount maps to 1.0, an equal premium to 0.0, parity to 0.5. None unless both legs are
    usable positive multiples (a negative/zero fwd P/E is not a meaningful valuation signal)."""
    if fwd_pe is None or median_fwd_pe is None or fwd_pe <= 0 or median_fwd_pe <= 0:
        return None
    discount = 1.0 - fwd_pe / median_fwd_pe        # >0 cheaper than peers, <0 richer
    return _clamp01(0.5 + 0.5 * discount / _VALUATION_CAP)


def _score_upside(upside: float | None) -> float | None:
    """Price-target upside (mean target / price − 1). +cap → 1.0, −cap → 0.0, flat → 0.5."""
    if upside is None:
        return None
    return _clamp01(0.5 + 0.5 * upside / _UPSIDE_CAP)


def _score_rating(rating_score: float | None) -> float | None:
    """Consensus rating is already signed in [-1, +1] (strongBuy=+1 … strongSell=−1).
    Linearly remap to [0, 1] (hold=0.5)."""
    if rating_score is None:
        return None
    return _clamp01(0.5 + 0.5 * rating_score)


def _score_revision(trend: float | None) -> float | None:
    """Estimate-revision momentum (fractional trend). +cap → 1.0, −cap → 0.0, flat → 0.5.
    The free producer does not emit this (paid feed, metron-ops#107) → None → dropped."""
    if trend is None:
        return None
    return _clamp01(0.5 + 0.5 * trend / _REVISION_CAP)


def _score_sentiment(sentiment: float | None) -> float | None:
    """News sentiment is already in [-1, +1]; linearly remap to [0, 1] (neutral=0.5)."""
    if sentiment is None:
        return None
    return _clamp01(0.5 + 0.5 * sentiment)


def compute(
    *,
    fwd_pe: float | None = None,
    median_fwd_pe: float | None = None,
    price_target_upside: float | None = None,
    consensus_score: float | None = None,
    estimate_revision_trend: float | None = None,
    news_sentiment: float | None = None,
) -> Attractiveness | None:
    """Blend the present components into a 0–100 attractiveness score with renormalized
    weights. Returns None when NO component is present (honest "—", never a fabricated 50).

    All inputs are already-loaded values from the spine (no I/O), so this is identical on the
    Holdings-list and tearsheet paths and trivially unit-testable.
    """
    raw: dict[str, float | None] = {
        "valuation": _score_valuation(fwd_pe, median_fwd_pe),
        "upside": _score_upside(price_target_upside),
        "rating": _score_rating(consensus_score),
        "revision": _score_revision(estimate_revision_trend),
        "sentiment": _score_sentiment(news_sentiment),
    }
    components = [
        Component(key=k, weight=WEIGHTS[k], sub_score=s)
        for k, s in raw.items()
        if s is not None
    ]
    if not components:
        return None
    total_w = sum(c.weight for c in components)
    if total_w <= 0:                                # defensive: all-zero weights → no signal
        return None
    blended = sum(c.weight * c.sub_score for c in components) / total_w
    return Attractiveness(
        score=round(100.0 * blended, 1),
        coverage=len(components),
        # Stable catalog order (matches WEIGHTS) so the gauge breakdown reads consistently.
        components=sorted(components, key=lambda c: list(WEIGHTS).index(c.key)),
    )
