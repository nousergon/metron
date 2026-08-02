"""Insights — the facet catalog and the deterministic ranker that selects from it.

The glance screen's ranked zones (metron-ops#248) and the Overview insights strip
(metron-ops#214) are both **ranker output, not layouts**. This package holds the two
halves that decision needs:

- ``registry`` — every observation Metron can make about a portfolio, each entry
  carrying the data sources it needs, the tier that packages it, and whether it is
  impersonal-factual (L1) or advice-flavoured (L2).
- ``ranker`` — a deterministic scorer over candidate observations, plus the floor
  behaviour for a quiet day.

**Metron decides what matters with code and says it with a model, never the reverse.**
Generation phrases what this package selects; it never selects. A ranker is auditable,
reproducible, and cannot hallucinate a priority — which is why selection lives here in
the open-core repo rather than in a prompt.
"""

from __future__ import annotations

from api.insights.ranker import (
    DEFAULT_LIMIT,
    DEFAULT_WEIGHTS,
    FLOOR_TEXT,
    Observation,
    RankWeights,
    Selection,
    rank,
    score,
    select,
    with_text,
)
from api.insights.registry import (
    CATALOG,
    FACET_BY_KEY,
    FAMILIES,
    LEVEL_L1,
    LEVEL_L2,
    LEVELS,
    Facet,
    candidate_facets,
    facets_in_family,
)

__all__ = [
    "CATALOG",
    "DEFAULT_LIMIT",
    "DEFAULT_WEIGHTS",
    "FACET_BY_KEY",
    "FAMILIES",
    "FLOOR_TEXT",
    "LEVELS",
    "LEVEL_L1",
    "LEVEL_L2",
    "Facet",
    "Observation",
    "RankWeights",
    "Selection",
    "candidate_facets",
    "facets_in_family",
    "rank",
    "score",
    "select",
    "with_text",
]
