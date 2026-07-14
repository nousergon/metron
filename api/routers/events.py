"""Self-hosted analytics event sink — ``POST /track`` + a minimal funnel read (metron-ops#34).

The generic, non-throwaway plumbing layer for Metron's beta-funnel instrumentation. No
third-party tracker (Cloudflare Web Analytics owns page-views); this is the EVENT tier.

``POST /track`` validates and inserts ONE event; new event types are just new
``event_name`` values, so the later product surfaces (#30 core read pages, #32 onboarding)
extend the funnel with no migration — they call this same endpoint. ``GET /track/funnel``
counts distinct sessions per event over a date range: the minimal read that proves the sink
works end to end (a dashboard UI is out of scope until there's a product surface to host it).

The events served here are the PRE-AUTH funnel (waitlist → signup → demo), so this router is
deliberately UNAUTHENTICATED and NOT tenant-scoped — there is no tenant/user yet. The web tier
is the only caller today (server-side, from a Server Action / route handler), so no CSRF/token
surface is exposed to the browser.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.db.session import get_session
from api.services import events as events_service

router = APIRouter(tags=["analytics"])


class TrackIn(BaseModel):
    # A lowercase snake_case identifier (e.g. "signup_submitted"). The service re-validates
    # the shape; ``min_length`` here just gives a cleaner 422 for the empty case.
    event_name: str = Field(min_length=1, max_length=80)
    # Anonymous funnel key from the web tier (an opaque cookie id); stitches a visitor's
    # pre-auth events together. Never a secret / never PII by contract.
    session_id: str = Field(min_length=1, max_length=64)
    # The authenticated user once known (post-signup); omitted for the pre-auth funnel.
    user_id: uuid.UUID | None = None
    # Arbitrary structured context (referrer, wedge, step, …).
    props: dict = Field(default_factory=dict)


class TrackOut(BaseModel):
    id: uuid.UUID
    event_name: str


class FunnelRowOut(BaseModel):
    event_name: str
    sessions: int


class FunnelOut(BaseModel):
    start: datetime | None
    end: datetime | None
    rows: list[FunnelRowOut]


@router.post("/track", response_model=TrackOut, status_code=201)
def track(body: TrackIn, session: Session = Depends(get_session)) -> TrackOut:
    """Record one analytics event. Returns 201 with the new event id; 422 on a
    missing/malformed ``event_name`` or ``session_id`` (the sink stays clean)."""
    try:
        event = events_service.record_event(
            session,
            event_name=body.event_name,
            session_id=body.session_id,
            user_id=body.user_id,
            props=body.props,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return TrackOut(id=event.id, event_name=event.event_name)


@router.get("/track/funnel", response_model=FunnelOut)
def funnel(
    start: datetime | None = Query(default=None, description="Inclusive lower bound on event ts (ISO 8601)."),
    end: datetime | None = Query(default=None, description="Exclusive upper bound on event ts (ISO 8601)."),
    session: Session = Depends(get_session),
) -> FunnelOut:
    """Distinct sessions per ``event_name`` over ``[start, end)`` — the minimal funnel read
    that proves the sink end to end. Both bounds optional (open = unbounded that side)."""
    rows = events_service.funnel_counts(session, start=start, end=end)
    return FunnelOut(
        start=start,
        end=end,
        rows=[FunnelRowOut(event_name=r.event_name, sessions=r.sessions) for r in rows],
    )
