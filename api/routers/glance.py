"""``GET /portfolios/{id}/glance`` — the whole glance screen in one round trip
(metron-ops#248 Stage A, #250). Composition lives in ``api.services.glance``."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.db.session import get_session
from api.routers.portfolios import _owned_portfolio
from api.services import glance as glance_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/portfolios", tags=["glance"])


def _axes(x_preview_tier: str | None, x_preview_feed: str | None) -> tuple[str, bool]:
    """The request's effective (tier, feed). The owner tier-simulator preview headers are
    honoured only when the simulator is on; an unknown preview tier falls back to the
    deployment default rather than 500-ing the screen."""
    preview_feed = None if x_preview_feed is None else x_preview_feed.strip().lower() == "true"
    tier, feed = ent.effective_axes(
        default_tier=settings.default_tier,
        feed_entitled=settings.feed_entitled,
        simulator=settings.tier_simulator,
        preview_tier=x_preview_tier,
        preview_feed=preview_feed,
    )
    if tier not in ent.TIER_BY_KEY:
        return settings.default_tier, settings.feed_entitled
    return tier, feed


@router.get("/{portfolio_id}/glance", response_model=glance_service.GlanceScreen)
def get_glance(
    response: Response,
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
    x_preview_tier: str | None = Header(default=None),
    x_preview_feed: str | None = Header(default=None),
) -> glance_service.GlanceScreen:
    """All six glance zones, per-row provenanced, with the render state as a field.

    Emits one per-request timing record — a ``glance composed`` (success) or ``glance
    failed`` (error) log line carrying the server-side duration, portfolio id, tier/feed
    axes and producer count (metron-ops-I327 O2 exit gate) — plus a ``Server-Timing``
    header, so the p95 ≤ 1.0 s gate is measured from a durable record, not asserted. The
    record is emitted on EVERY request, success or failure: a raised exception is logged
    with its duration before propagating (never swallowed — FastAPI still turns it into
    a 500), so an elevated error rate cannot hide as missing latency data."""
    t0 = time.perf_counter()
    tier, feed = _axes(x_preview_tier, x_preview_feed)
    # The tier can originate from the ``X-Preview-Tier`` request header, so only a value
    # copied from the closed ``TIERS`` tuple (never the request string itself) reaches the log line (log-injection guard, CodeQL
    # py/log-injection); anything else is logged as the literal "unknown".
    log_tier = next((t.key for t in ent.TIERS if t.key == tier), "unknown")
    feat = ent.feature_state(
        "glance",
        default_tier=settings.default_tier,
        feed_entitled=settings.feed_entitled,
        simulator=settings.tier_simulator,
        preview_tier=x_preview_tier,
        preview_feed=None if x_preview_feed is None else x_preview_feed.strip().lower() == "true",
    )
    if not feat["available"]:
        raise HTTPException(status_code=403, detail=f"The glance screen is not available on this plan ({feat['reason']}).")
    try:
        screen = glance_service.compose(session, portfolio, tier=tier, feed_enabled=feed)
    except Exception:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        log.error(
            "glance failed portfolio=%s duration_ms=%.1f tier=%s feed=%s",
            portfolio.id, duration_ms, log_tier, bool(feed), exc_info=True,
        )
        raise
    duration_ms = (time.perf_counter() - t0) * 1000.0
    response.headers["Server-Timing"] = f"glance;dur={duration_ms:.1f}"
    log.info(
        "glance composed portfolio=%s duration_ms=%.1f state=%s tier=%s feed=%s degraded=%d producers=%d zones_ms=%s",
        portfolio.id,
        duration_ms,
        screen.state,
        log_tier,
        bool(feed),
        len(screen.degraded),
        screen.coverage.produced_facets,
        screen.timings_ms,
    )
    return screen
