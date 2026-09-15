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

    Emits one ``glance composed`` log line with the server-side duration (and a
    ``Server-Timing`` header) so the p95 ≤ 1.0 s exit gate is measured, not asserted."""
    t0 = time.perf_counter()
    tier, feed = _axes(x_preview_tier, x_preview_feed)
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
    screen = glance_service.compose(session, portfolio, tier=tier, feed_enabled=feed)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    response.headers["Server-Timing"] = f"glance;dur={duration_ms:.1f}"
    log.info(
        "glance composed portfolio=%s duration_ms=%.1f state=%s tier=%s feed=%s degraded=%d zones_ms=%s",
        portfolio.id,
        duration_ms,
        screen.state,
        tier,
        feed,
        len(screen.degraded),
        screen.timings_ms,
    )
    return screen
