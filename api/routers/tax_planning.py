"""Read-only ``tax-planning`` surface — the telos year-round tax projection.

Serves the owner's ``TaxProjection`` artifact (projected full-year federal
liability, §6654 safe-harbor summary, quarterly 1040-ES installments with
paid/overdue/upcoming flags, and the "pay $X by DATE" headline) produced by
the telos tax engine and cached last-good under ``cache/tax_projection.json``
(``ingestion.tax_projection_store``). metron-ops#133; telos plan §6.5/§11-A.

Gating: rides the existing ``tax`` feature (deps ledger+broker — same
entitlement as the /tax page this renders on). Not-entitled callers get the
structured ``{available: false, reason, required_tier}`` payload, mirroring
``research_intel`` — never a 500, never leaked data.

Metron never imports telos code: the versioned JSON artifact is the entire
coupling (M0 contract discipline). An unsupported artifact schema MAJOR is
surfaced as a named ``schema_error`` (fail loud on the page), while a merely
missing artifact is ``stale`` (explicit empty state).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header

from api import entitlements
from api.config import settings
from portfolio_analytics.ingestion.tax_projection_store import (
    load_tax_projection,
    schema_error,
)

router = APIRouter(prefix="/tax-planning", tags=["tax-planning"])

FEATURE = "tax"


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
def get_tax_planning(
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> dict:
    """The cached tax projection for the active tier.

    Returns ``{available, reason, required_tier, stale, schema_error, projection}``:
    - not entitled → ``available=false`` + upsell ``reason``/``required_tier``;
    - entitled, no cached artifact → ``available=true``, ``stale=true``,
      ``projection=null`` (the page renders an explicit "no projection yet");
    - entitled, artifact with an unsupported schema MAJOR → ``schema_error``
      names the mismatch, ``projection=null`` (fail loud, not mis-render);
    - entitled + readable artifact → the projection dict passes through as-is.
    """
    feat = _feature_state(x_preview_tier, x_preview_feed)
    if not feat["available"]:
        return {
            "available": False,
            "reason": feat["reason"],
            "required_tier": feat["required_tier"],
            "stale": None,
            "schema_error": None,
            "projection": None,
        }

    projection = load_tax_projection(path=Path(settings.tax_projection_path))
    if projection is None:
        return {
            "available": True,
            "reason": None,
            "required_tier": None,
            "stale": True,
            "schema_error": None,
            "projection": None,
        }

    err = schema_error(projection)
    if err is not None:
        return {
            "available": True,
            "reason": None,
            "required_tier": None,
            "stale": False,
            "schema_error": err,
            "projection": None,
        }

    return {
        "available": True,
        "reason": None,
        "required_tier": None,
        "stale": False,
        "schema_error": None,
        "projection": projection,
    }
