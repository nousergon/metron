"""Read-only ``research_intel`` surface — neutral market intel for the paid tier.

Serves the global research-intel snapshot (regime + narrative, sector ratings/modifiers,
market breadth, per-holding attractiveness + generic thesis) that the crucible-research
run publishes to ``research_intel/latest.json`` and the daily refresh caches last-good
(``ingestion.research_intel_store``). EPIC config#1499 Phase 1 / metron-ops#117.

Gating: the ``research_intel`` entitlement is packaged to the paid **AI Advisor** tier
only (``api.entitlements``). A non-entitled caller gets a structured
``{available: false, reason, required_tier}`` upsell payload (never a 500, never leaked
intel) — mirroring the ``/portfolios/{id}/risk`` not-computable convention. Effective
tier/feed resolve through the canonical ``entitlements.feature_state`` helper, honoring
the owner tier-simulator's ``X-Preview-*`` headers exactly like ``/meta/entitlements``.

No LLM here (the advisor engine is the private Phase-2 plugin). This is a pure data read.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query

from api import entitlements
from api.config import settings
from portfolio_analytics.ingestion.research_intel_store import load_research_intel

router = APIRouter(prefix="/research-intel", tags=["research-intel"])

FEATURE = "research_intel"


def _feature_state(x_preview_tier: str | None, x_preview_feed: str | None) -> dict:
    """One feature's entitlement for the request's effective tier/feed (canonical helper)."""
    preview_feed = None
    if x_preview_feed is not None:
        preview_feed = x_preview_feed.strip().lower() == "true"
    return entitlements.feature_state(
        FEATURE,
        default_tier=settings.default_tier,
        feed_entitled=settings.feed_entitled,
        simulator=settings.tier_simulator,
        preview_tier=x_preview_tier,
        preview_feed=preview_feed,
    )


@router.get("")
def get_research_intel(
    tickers: str | None = Query(
        default=None,
        description="Comma-separated tickers to scope the attractiveness map to "
        "(typically the caller's holdings). Omit for the full universe.",
    ),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> dict:
    """Neutral research intel for the active tier.

    Returns ``{available, reason, required_tier, stale, intel}``:
    - not entitled → ``available=false`` + upsell ``reason``/``required_tier``, ``intel=null``;
    - entitled but no cached artifact yet → ``available=true``, ``stale=true``, ``intel=null``;
    - entitled + cached → ``available=true``, ``stale=false``, and ``intel`` with the global
      regime/breadth/sector context plus the (optionally ticker-scoped) attractiveness map.
    """
    feat = _feature_state(x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return {
            "available": False,
            "reason": feat["reason"],
            "required_tier": feat["required_tier"],
            "stale": None,
            "intel": None,
        }

    snapshot = load_research_intel()
    if snapshot is None:
        return {"available": True, "reason": None, "required_tier": None, "stale": True, "intel": None}

    wanted = tickers.split(",") if tickers else None
    intel = snapshot.to_dict()
    # Scope the (potentially large) attractiveness map to the requested tickers, leaving
    # the global regime/sector/breadth context intact.
    intel["attractiveness"] = {k: v.to_dict() for k, v in snapshot.for_tickers(wanted).items()}
    intel.pop("error", None)  # never surface the internal fetch-error field on the read path
    return {"available": True, "reason": None, "required_tier": None, "stale": False, "intel": intel}
