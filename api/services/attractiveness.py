"""Composite per-holding **attractiveness score** — SOTA 6-pillar cross-sectional blend.

Ports the institutional method from crucible-research ``scoring/universe_board.py``
(schema v3) via ``nousergon_lib.quant.attractiveness``: sector-neutral pillar
percentiles from NE factor profiles → per-pillar cross-sectional z-scores →
coverage-renormalized weighted blend → terminal cross-sectional percentile (0–100).

Design:
  * The cross-section is the full ~900-name scanner universe in
    ``factors/profiles/latest.json`` — NOT the user's holdings in isolation — so
    scores are byte-identical to the NE console universe board.
  * Each pillar is a 0–100 within-sector percentile from the factor substrate.
  * A pillar absent for a ticker is dropped and remaining weights renormalize.
  * Holdings outside the scanner universe get honest ``None`` (never fabricated).

Pure lookup after the universe blend is computed once per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nousergon_lib.quant.attractiveness import (
    PILLAR_ORDER,
    PILLAR_TO_FACTOR_KEY,
    attractiveness_from_factor_profiles,
    normalize_pillar_weights,
)

from api.services import factor_profiles as factor_profiles_service


@dataclass
class PillarComponent:
    """One inspectable pillar line — drives the tearsheet gauge + Holdings band."""

    key: str
    weight: float
    score: float
    contribution: float | None = None


@dataclass
class Attractiveness:
    """A holding's SOTA attractiveness + its fully-inspectable pillar breakdown."""

    score: float | None
    coverage: int
    pillars: list[PillarComponent] = field(default_factory=list)


def _build_result(
    ticker: str,
    profile: dict,
    blended: dict,
    catalog_weights: dict[str, float],
) -> Attractiveness | None:
    score = blended.get("attractiveness_score")
    contribs = blended.get("pillar_contributions") or {}
    pillars: list[PillarComponent] = []
    for p in PILLAR_ORDER:
        raw = profile.get(PILLAR_TO_FACTOR_KEY[p])
        if raw is None:
            continue
        try:
            pillar_score = float(raw)
        except (TypeError, ValueError):
            continue
        pillars.append(
            PillarComponent(
                key=p,
                weight=catalog_weights.get(p, 0.0),
                score=round(pillar_score, 1),
                contribution=contribs.get(p),
            )
        )
    if score is None and not pillars:
        return None
    return Attractiveness(
        score=round(score, 1) if score is not None else None,
        coverage=len(pillars),
        pillars=pillars,
    )


def compute_universe(
    *,
    profiles_reader=None,
    weights_reader=None,
) -> dict[str, Attractiveness]:
    """Blend the full scanner-universe factor profiles into per-ticker attractiveness."""
    snap = factor_profiles_service.load_factor_profiles(reader=profiles_reader)
    if not snap.by_ticker:
        return {}
    raw_weights = factor_profiles_service.load_pillar_weights(reader=weights_reader)
    catalog_weights = normalize_pillar_weights(raw_weights)
    blended = attractiveness_from_factor_profiles(snap.by_ticker, pillar_weights=raw_weights)
    return {
        ticker.upper(): result
        for ticker, rec in blended.items()
        if (result := _build_result(ticker, snap.by_ticker.get(ticker, {}), rec, catalog_weights))
        is not None
    }


def lookup(
    yf_symbol: str,
    universe: dict[str, Attractiveness],
) -> Attractiveness | None:
    """Resolve one holding's attractiveness from a pre-computed universe map."""
    return universe.get(yf_symbol.upper())
