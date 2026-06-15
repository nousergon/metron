"""Meta endpoints — prove the shared engine is wired in and advertise capabilities."""

from __future__ import annotations

import importlib.metadata

from fastapi import APIRouter, HTTPException

import portfolio_analytics
from api import entitlements
from api.config import settings
from api.plugins import active_plugins

router = APIRouter(prefix="/meta", tags=["system"])


def _engine_version() -> str:
    try:
        return importlib.metadata.version("portfolio-analytics")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - editable-install edge
        return getattr(portfolio_analytics, "__version__", "unknown")


@router.get("")
def meta() -> dict:
    """Report the engine version and the descriptive analytics this product offers.

    The trust posture is part of the contract: no AI, no ads/trackers, no advice.
    """
    return {
        "engine": "portfolio-analytics",
        "engine_version": _engine_version(),
        "capabilities": [
            "performance",      # TWR + MWR/XIRR
            "attribution",      # contribution + Brinson
            "risk",             # factor exposures, tracking error, look-through
            "scenarios",        # historical replays, factor shocks, VaR/CVaR
            "income",           # dividends, projected income, yield-on-cost
            "tax",              # realized/unrealized lots, ST/LT, loss-harvest info
        ],
        "posture": {
            "ai": False,
            "ads_or_trackers": False,
            "advice": False,
            "read_only": True,
        },
    }


@router.get("/plugins")
def plugins() -> list[dict]:
    """Nav metadata for every active out-of-tree plugin (empty on the public tier).

    The web reads this to render premium nav links + gate premium pages — a surface
    appears only when its plugin is installed AND its ``enabled()`` gate is on. On a
    stock public/self-host deploy (no metron-ops) this is always ``[]``, so the
    no-AI / no-advice posture above holds without the frontend knowing about plugins.
    """
    return [
        {"id": p.nav.id, "label": p.nav.label, "href": p.nav.href, "tier": p.nav.tier}
        for p in active_plugins()
    ]


@router.get("/entitlements")
def entitlements_endpoint(
    preview_tier: str | None = None,
    preview_feed: bool | None = None,
) -> dict:
    """Resolve the active tier's per-feature availability.

    Effective tier = this deployment's ``default_tier`` and ``feed`` = whether the
    licensed market-data feed is provisioned (``market_data_sync_enabled``). When
    the **tier simulator** is on (``tier_simulator`` — owner-only, never on the
    public product), ``?preview_tier=`` / ``?preview_feed=`` override them so the
    personal build can render any product level (Beta / Pro / Research+ / Base) and
    toggle the feed to see exactly what each level excludes. Simulator off → the
    preview params are ignored (a public caller can't re-scope its entitlements).
    """
    tier = settings.default_tier
    feed = settings.market_data_sync_enabled
    if settings.tier_simulator:
        if preview_tier is not None:
            tier = preview_tier
        if preview_feed is not None:
            feed = preview_feed
    try:
        result = entitlements.resolve(tier, feed_enabled=feed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    result["simulator"] = settings.tier_simulator
    return result
