"""Meta endpoints — prove the shared engine is wired in, advertise capabilities,
and report system-wide data freshness (metron-ops#220 provenance surface)."""

from __future__ import annotations

import importlib.metadata
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import portfolio_analytics
from api import entitlements
from api.config import settings
from api.db import models
from api.db.session import get_session
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
        # Connector capabilities the UI gates on — server-side stored credentials enable a
        # one-click sync (no paste). metron-ops#82.
        "connectors": {
            "flex_stored": bool(settings.flex_token and settings.flex_query_id),
            "snaptrade_personal": settings.snaptrade_personal,
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
    licensed market-data feed is provisioned for entitlement (``feed_entitled`` —
    decoupled from the S3 ``market_data_sync_enabled`` infra toggle per metron-ops#43).
    When the **tier simulator** is on (``tier_simulator`` — owner-only, never on the
    public product), ``?preview_tier=`` / ``?preview_feed=`` override them so the
    personal build can render any product level (Beta / Pro / Research+ / Base) and
    toggle the feed to see exactly what each level excludes. Simulator off → the
    preview params are ignored (a public caller can't re-scope its entitlements).
    """
    tier = settings.default_tier
    feed = settings.feed_entitled
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


@router.get("/status")
def system_status(session: Session = Depends(get_session)) -> dict:
    """System-wide data-surface freshness (metron-ops#220 — provenance surface).

    Reports what data is available, how fresh each source is, and the deployment's
    entitlement state — the machine-readable /status surface every served analytics
    payload carries provenance markers for individually (the ``as_of`` / ``last_price_date``
    / ``estimated`` / ``stale`` fields on `HoldingOut`, `IntradayStatusOut`, `PerformanceOut`,
    etc.). Reconciliation-run and open-break-count fields are explicitly gated on the
    layer-1 break store (metron-ops#210 layer 1) and will be added once it lands.
    """
    # Account counts by broker source — the number of distinct connected accounts
    # across all tenants, plus a breakdown by broker type.
    total_accounts = session.scalar(select(func.count(models.Account.id)))
    broker_counts = dict(
        session.execute(
            select(models.Account.broker, func.count(models.Account.id))
            .group_by(models.Account.broker)
        ).all()
    )

    # Most recent NAV snapshot date across all portfolios — a proxy for
    # "when was the last successful price refresh / EOD valuation?"
    latest_nav = session.scalar(
        select(models.NavSnapshot.snap_date)
        .order_by(models.NavSnapshot.snap_date.desc())
        .limit(1)
    )

    # Total portfolio count
    total_portfolios = session.scalar(select(func.count(models.Portfolio.id)))

    return {
        "engine": "portfolio-analytics",
        "engine_version": _engine_version(),
        "deployment": {
            "tier": settings.default_tier,
            "feed_entitled": settings.feed_entitled,
            "market_data_sync_enabled": settings.market_data_sync_enabled,
            "tier_simulator": settings.tier_simulator,
        },
        "connectors": {
            "flex_stored": bool(settings.flex_token and settings.flex_query_id),
            "snaptrade_personal": settings.snaptrade_personal,
        },
        "data_freshness": {
            "latest_eod_valuation_date": latest_nav.isoformat() if latest_nav else None,
            "latest_eod_valuation_source": "nav_snapshots — EOD close cache after price refresh",
        },
        "accounts": {
            "total": total_accounts or 0,
            "by_broker": broker_counts,
        },
        "portfolios": {
            "total": total_portfolios or 0,
        },
        "reconciliation": {
            "available": False,
            "note": "Layer 1 (break store) not yet deployed — reconciliation-run and open-break-count fields will be added once metron-ops#210 layer 1 lands.",
        },
    }
