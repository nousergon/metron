"""The deterministic insight ranker — selection, which is the product.

Roughly eighty registered facets against a real portfolio produce a few hundred
candidate observations a day. A surface has five slots. **Ranking is therefore the
product, and it is deterministic by construction:**

- The same candidates, weights and shown-history always produce the same slate. There is
  no sampling, no clock read, and no model call in this module.
- Ties break on ``facet_key``, so ordering is stable rather than dependent on the order
  candidates happened to be generated in.
- Every score is reconstructible from the ``Observation`` fields, which is what makes a
  slate auditable after the fact — the Transparency principle at a product surface.

**Generation phrases what this module selects; it never selects.** A ranker cannot
hallucinate a priority.

## The floor is a feature, not a fallback

When nothing clears ``floor_threshold``, ``select`` returns a single plain statement that
nothing is unusual — it does **not** pad the slate with the least-boring trivia
available. Filling slots for the sake of fullness is how a daily surface loses trust,
and trust is the retention bar. A confident "nothing to see" is the same discipline as
rendering the integrity zone on the healthy path.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

FLOOR_TEXT = "Nothing unusual today — your portfolio moved with its factors."

# A slate this size fills the glance screen's ranked zones (metron-ops#248 zones 3–5).
DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class Observation:
    """One candidate factual statement about a portfolio, ready to be ranked.

    The scoring inputs are deliberately **normalised, unitless and producer-supplied**:
    the ranker compares a tax-lot observation against a concentration observation, so it
    cannot reason in dollars or percent directly. Each producer is responsible for
    mapping its own domain onto 0–1, which keeps domain knowledge with the service that
    has it instead of centralising a table of magic thresholds here.

    - ``materiality`` — how much money this concerns, relative to the portfolio.
    - ``deviation`` — how far this sits from the portfolio's own history. A 40% tech
      weight is unremarkable for someone who has always held 40% tech.
    - ``rule_breach`` — a **user-authored** rule is violated. Weighted highest because it
      is the one input the user explicitly asked to be told about, and because it is
      arithmetic against their own number rather than a judgement.
    - ``urgency`` — a date is approaching or passing (a lot crossing one year, an
      ex-dividend date).

    ``as_of`` is carried through unscored: nothing reaches a surface unprovenanced.
    """

    facet_key: str
    text: str
    as_of: str
    materiality: float = 0.0
    deviation: float = 0.0
    urgency: float = 0.0
    rule_breach: bool = False

    def __post_init__(self) -> None:
        for name in ("materiality", "deviation", "urgency"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within 0..1, got {value!r} for facet {self.facet_key!r}")
        if not self.text.strip():
            raise ValueError(f"observation for facet {self.facet_key!r} has empty text")
        if not self.as_of.strip():
            raise ValueError(f"observation for facet {self.facet_key!r} has no as-of provenance")


@dataclass(frozen=True)
class RankWeights:
    """Scoring weights.

    Defaults live in the open-core repo so a self-host deployment ranks sensibly out of
    the box. A tuned policy may be supplied by the private ``metron_ext`` overlay by
    passing its own instance — the interface is the extension point, so a proprietary
    ranking policy never requires forking this module.

    ``novelty_penalty`` is subtracted when a facet was already shown recently. It is a
    penalty rather than an exclusion on purpose: a genuinely material fact should be
    allowed to repeat, but it must out-score a fresh one to do so.
    """

    materiality: float = 1.0
    deviation: float = 0.8
    urgency: float = 0.6
    rule_breach: float = 1.2
    novelty_penalty: float = 0.5


DEFAULT_WEIGHTS = RankWeights()


@dataclass(frozen=True)
class Selection:
    """A ranked slate plus whether the floor fired.

    ``is_floor`` is surfaced rather than inferred from the contents so a caller never has
    to pattern-match on ``FLOOR_TEXT`` to know what it is looking at.
    """

    observations: tuple[Observation, ...]
    is_floor: bool


def score(
    observation: Observation,
    *,
    weights: RankWeights = DEFAULT_WEIGHTS,
    already_shown: Iterable[str] = (),
) -> float:
    """This observation's rank score. Pure, and reconstructible from its inputs."""
    shown = set(already_shown)
    total = (
        weights.materiality * observation.materiality
        + weights.deviation * observation.deviation
        + weights.urgency * observation.urgency
        + (weights.rule_breach if observation.rule_breach else 0.0)
    )
    if observation.facet_key in shown:
        total -= weights.novelty_penalty
    return total


def rank(
    observations: Sequence[Observation],
    *,
    weights: RankWeights = DEFAULT_WEIGHTS,
    already_shown: Iterable[str] = (),
    limit: int = DEFAULT_LIMIT,
) -> tuple[Observation, ...]:
    """Order ``observations`` best-first and take ``limit``.

    Deterministic: sorted by descending score, ties broken on ``facet_key`` so the result
    never depends on the order candidates were generated in.
    """
    if limit < 0:
        raise ValueError(f"limit must not be negative, got {limit}")
    shown = tuple(already_shown)
    ordered = sorted(
        observations,
        key=lambda o: (-score(o, weights=weights, already_shown=shown), o.facet_key),
    )
    return tuple(ordered[:limit])


def select(
    observations: Sequence[Observation],
    *,
    weights: RankWeights = DEFAULT_WEIGHTS,
    already_shown: Iterable[str] = (),
    limit: int = DEFAULT_LIMIT,
    floor_threshold: float = 0.25,
    as_of: str = "",
) -> Selection:
    """Rank, and fall back to the floor statement when nothing is worth saying.

    ``floor_threshold`` is the score below which an observation is not worth a slot. When
    **no** candidate clears it the slate is replaced — not topped up — with the single
    floor statement, because a quiet day honestly reported beats five padded slots.

    ``as_of`` provenances the floor statement itself; it defaults to the best-scoring
    candidate's ``as_of`` so the floor is never rendered unprovenanced even when every
    candidate was discarded.

    **Raises** when there are no candidates *and* no ``as_of``: "nothing unusual today"
    is a claim about a moment, and a caller that cannot name the moment does not know
    what data it is speaking for. Emitting it unprovenanced would be the one case where
    a surface says something it cannot stand behind, so this fails loudly instead.
    """
    shown = tuple(already_shown)
    ranked = rank(observations, weights=weights, already_shown=shown, limit=limit)
    kept = tuple(o for o in ranked if score(o, weights=weights, already_shown=shown) >= floor_threshold)
    if kept:
        return Selection(observations=kept, is_floor=False)
    floor_as_of = as_of or (ranked[0].as_of if ranked else "")
    if not floor_as_of.strip():
        raise ValueError(
            "cannot emit the floor statement without provenance: pass as_of= when there are no candidates"
        )
    return Selection(
        observations=(Observation(facet_key="floor", text=FLOOR_TEXT, as_of=floor_as_of),),
        is_floor=True,
    )


def with_text(observation: Observation, text: str) -> Observation:
    """A copy of ``observation`` carrying generated phrasing.

    The seam between deterministic selection and generated wording: a surface ranks
    first, then hands the winning slate to generation for phrasing. Scoring inputs are
    preserved unchanged, so re-ranking a phrased slate yields the same order — a
    generated sentence can never alter its own priority.
    """
    return replace(observation, text=text)
