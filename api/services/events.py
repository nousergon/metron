"""Self-hosted analytics event sink + funnel read (metron-ops#34).

The generic plumbing layer behind ``/track``: ``record_event`` inserts one event and
``funnel_counts`` answers "how many distinct sessions fired each event over a window".
Both are intentionally event-type-agnostic — a new funnel step is a new ``event_name``
string, not a code or schema change here — so the later product surfaces (#30 core read
pages, #32 onboarding) just call ``track(...)`` and this module already stores + counts
them. No third-party tracker is involved (Cloudflare Web Analytics owns page-views);
this is the EVENT-level tier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models

# Event-name shape guard. Events are a controlled vocabulary of snake_case identifiers
# (``signup_submitted``, ``demo_viewed``, …); rejecting anything else keeps the funnel
# query's GROUP BY over a clean key space and stops a malformed/injected name from
# polluting the store. Length is capped to the column width.
_MAX_EVENT_NAME = 80
_MAX_SESSION_ID = 64


def _clean_name(event_name: str) -> str:
    name = (event_name or "").strip()
    if not name:
        raise ValueError("event_name is required")
    if len(name) > _MAX_EVENT_NAME:
        raise ValueError(f"event_name exceeds {_MAX_EVENT_NAME} chars")
    # snake_case identifier: a letter, then letters/digits/underscores. Deliberately strict
    # so the vocabulary stays legible and greppable across the funnel.
    if not name.replace("_", "").isalnum() or not name[0].isalpha() or not name.islower():
        raise ValueError("event_name must be a lowercase snake_case identifier")
    return name


def record_event(
    session: Session,
    *,
    event_name: str,
    session_id: str,
    user_id: uuid.UUID | None = None,
    props: dict | None = None,
) -> models.Event:
    """Insert one analytics event. ``session_id`` is the anonymous funnel key (required —
    even a pre-auth visitor has one); ``user_id`` is attached only once the visitor is
    known. ``props`` is an arbitrary JSON payload (defaults to ``{}``). Raises ``ValueError``
    on a missing/malformed ``event_name`` or ``session_id`` so the endpoint can 4xx it."""
    name = _clean_name(event_name)
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if len(sid) > _MAX_SESSION_ID:
        raise ValueError(f"session_id exceeds {_MAX_SESSION_ID} chars")
    if props is not None and not isinstance(props, dict):
        raise ValueError("props must be an object")
    event = models.Event(
        event_name=name,
        session_id=sid,
        user_id=user_id,
        props=props or {},
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@dataclass
class FunnelCount:
    event_name: str
    sessions: int  # distinct session_id count for this event over the window


def funnel_counts(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[FunnelCount]:
    """Distinct sessions per ``event_name`` over the ``[start, end)`` window (both bounds
    optional — an open bound means "unbounded on that side"). This is the minimal read that
    proves the sink works end to end: it is the raw material of a funnel (waitlist → signup
    → activation) without baking in any particular funnel's step order — the caller sequences
    the event names it cares about. Ordered by descending session count for a stable,
    human-readable result."""
    stmt = select(
        models.Event.event_name,
        func.count(func.distinct(models.Event.session_id)),
    )
    if start is not None:
        stmt = stmt.where(models.Event.ts >= start)
    if end is not None:
        stmt = stmt.where(models.Event.ts < end)
    stmt = stmt.group_by(models.Event.event_name).order_by(
        func.count(func.distinct(models.Event.session_id)).desc(),
        models.Event.event_name,
    )
    return [FunnelCount(event_name=name, sessions=int(n)) for name, n in session.execute(stmt).all()]
