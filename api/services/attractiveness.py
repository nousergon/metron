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

Pure lookup after the universe blend is computed once per request (cached to avoid
redundant S3 reads and blending computation within the same request).
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from nousergon_lib.quant.attractiveness import (
    PILLAR_ORDER,
    PILLAR_TO_FACTOR_KEY,
    attractiveness_from_factor_profiles,
    normalize_pillar_weights,
)

from api.services import factor_profiles as factor_profiles_service

_COMPUTE_CACHE_TTL_S = 3600.0  # 1 hour; matches factor profile update cadence
_compute_universe_cache: dict[str, object | None] = {}  # None = empty universe, else dict[str, Attractiveness]
_compute_universe_cache_time: float = 0.0

# Request-scoped cache (per contextvars — thread-safe across concurrent requests)
_request_universe_cache: ContextVar[dict[str, object] | None] = ContextVar(
    "_request_universe_cache", default=None
)


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
    """Blend the full scanner-universe factor profiles into per-ticker attractiveness.

    Caching strategy (multi-tier to avoid redundant S3 reads):
    1. Request-scoped (contextvars): multiple compute_universe() calls within the same
       request (e.g., metrics_enrichment.py + tearsheet.py) read S3 once per request.
    2. Module-level (1-hour TTL): across requests, reuse computed universe for 1 hour.

    When custom readers are supplied (tests), bypass caching entirely."""
    if profiles_reader is not None or weights_reader is not None:
        # Test path: always compute fresh.
        return _compute_universe_uncached(profiles_reader, weights_reader)

    # Request-scoped cache (production path, step 1: within-request dedup).
    req_cache = _request_universe_cache.get()
    if req_cache is not None:
        return req_cache if req_cache else {}

    # Module-level cache (production path, step 2: across-request 1-hour dedup).
    global _compute_universe_cache, _compute_universe_cache_time
    now = time.time()
    if now - _compute_universe_cache_time < _COMPUTE_CACHE_TTL_S and "" in _compute_universe_cache:
        cached = _compute_universe_cache[""]
        result = cached if cached is not None else {}
        _request_universe_cache.set(result)
        return result

    # Cache miss: compute fresh, populate both caches.
    result = _compute_universe_uncached(None, None)
    now = time.time()
    _compute_universe_cache_time = now
    _compute_universe_cache[""] = result if result else None
    _request_universe_cache.set(result if result else {})
    return result


def _compute_universe_uncached(
    profiles_reader=None,
    weights_reader=None,
) -> dict[str, Attractiveness]:
    """Compute universe blend without caching — internal helper."""
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


def clear_cache() -> None:
    """Clear module-level + request-scoped caches — for tests and request cleanup."""
    global _compute_universe_cache, _compute_universe_cache_time
    _compute_universe_cache.clear()
    _compute_universe_cache_time = 0.0
    _request_universe_cache.set(None)


def clear_request_cache() -> None:
    """Clear request-scoped cache only (called by request lifecycle middleware)."""
    _request_universe_cache.set(None)
