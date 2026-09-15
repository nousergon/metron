"""External user demo (metron-ops-I310) — invites, pinned sessions, locked cards, counters.

An invited viewer redeems a **single-use invite code** and receives an **opaque,
server-side session token**. The web tier keeps the token in an httpOnly cookie and sends
it to the API as ``X-Demo-Session``. A request carrying that header is:

1. **Route-restricted, default deny** (``is_route_allowed``, applied by the
   ``_external_demo_pin`` middleware in ``api/main.py``): read-only methods, the demo
   household's own paths minus the advice routes, and a closed set of global reads. Every
   other route — the advice routers, plugin routes under ``/ext``, operator endpoints —
   is refused before routing, so a route added later is denied until listed here.
2. **Entitlement-pinned** (``api.entitlements`` request pin): the no-advice feature set
   (``_PRO`` minus the advice features) with the licensed feed ON, whatever the
   deployment's tier, feed setting or tier-simulator headers say.
3. **Tenant-resolved** only through ``identity.require_tenant_id``, which verifies the
   token against ``external_demo_sessions`` and returns the read-only demo tenant.

The pin keys off header PRESENCE and only ever narrows what a request sees; the token is
verified wherever data is authorised. Nothing here reaches an unauthenticated route.

Why invite codes and not the shared magic-link flow: a magic link authenticates a real
identity, which JIT-provisions a writable personal workspace (``identity.
resolve_identity_user``) and needs the allowlist in the separate auth service. The demo
needs neither an identity nor a workspace; a code-minted session lands on the existing
read-only demo tenant, stores no email, is revocable per row, and is killed wholesale by
the release flag.

**Release gate:** ``settings.external_demo_released`` (default False) disables invite
creation, redemption and every existing session. Nothing in this repo flips it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api import entitlements as ent
from api.config import settings
from api.db import models
from api.services import events as events_service
from api.services.demo_household import DEMO_HOUSEHOLD_PORTFOLIO_ID

SESSION_HEADER = "x-demo-session"

# ── Locked cards ─────────────────────────────────────────────────────────────
ADVICE_STATUS = "In development · available after regulatory review"
FEED_STATUS = "In development · available after data-licensing review"


@dataclass(frozen=True)
class LockedCard:
    key: str
    name: str
    line: str
    status: str


ADVICE_CARDS: tuple[LockedCard, ...] = (
    LockedCard("deploy_cash", "Deploy cash", "Sizes new cash across securities scored by the technical rating.", ADVICE_STATUS),
    LockedCard(
        "market_board",
        "Market board",
        "Technical attractiveness across held and watchlist securities, with its graded track record.",
        ADVICE_STATUS,
    ),
    LockedCard("research_intel", "Research intel", "Market regime, sector ratings and breadth from the research pipeline.", ADVICE_STATUS),
    LockedCard("alpha_engine", "Alpha Engine overlay", "Signals from the Alpha Engine research system shown beside holdings.", ADVICE_STATUS),
    LockedCard("intelligence", "Intelligence", "Conversational analysis grounded in the portfolio's own facts.", ADVICE_STATUS),
)

# Shown only when the fallback (``external_demo_feed_features_locked``) re-locks them.
FEED_CARDS: tuple[LockedCard, ...] = (
    LockedCard("risk", "Risk", "Factor exposures, tracking error and look-through risk.", FEED_STATUS),
    LockedCard("attribution", "Attribution", "Brinson attribution of returns by sector and selection.", FEED_STATUS),
    LockedCard("benchmark", "Benchmark", "Portfolio return measured against a benchmark index.", FEED_STATUS),
    LockedCard("scenarios", "Scenarios", "Historical replays and factor shocks applied to current holdings.", FEED_STATUS),
    LockedCard("calendar", "Calendar", "Upcoming earnings dates for held securities.", FEED_STATUS),
    LockedCard("indices", "Market indices", "Intraday moves of the major index proxies.", FEED_STATUS),
    LockedCard("etf_lookthrough", "ETF look-through", "Underlying exposures of held funds.", FEED_STATUS),
)
if {c.key for c in FEED_CARDS} != ent.FEED_DERIVED_FEATURES:
    raise ValueError("FEED_CARDS must cover exactly entitlements.FEED_DERIVED_FEATURES")


def locked_cards() -> tuple[LockedCard, ...]:
    """The locked cards an external-demo viewer sees under the current configuration."""
    if settings.external_demo_feed_features_locked:
        return ADVICE_CARDS + FEED_CARDS
    return ADVICE_CARDS


ALL_CARD_KEYS: frozenset[str] = frozenset(c.key for c in ADVICE_CARDS + FEED_CARDS)


def pin() -> ent.Pin:
    """The entitlement pin every external-demo request resolves against."""
    return ent.Pin(
        features=ent.external_demo_features(feed_features_locked=settings.external_demo_feed_features_locked),
        feed_enabled=True,
    )


# ── Route policy (default deny) ──────────────────────────────────────────────
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GLOBAL_READS = frozenset({
    "/me",
    "/meta",
    "/meta/entitlements",
    "/meta/plugins",
    "/portfolios",
    "/macro",
    "/indices/intraday",
    "/external-demo/locked-cards",
})
_POST_ALLOWED = frozenset({"/external-demo/taps"})
_HOUSEHOLD_PATH = re.compile(rf"^/portfolios/{re.escape(str(DEMO_HOUSEHOLD_PORTFOLIO_ID))}(?:/.*)?$")
# Advice routes under the household path. Refused even though the path matches.
_ADVICE_SUFFIX = re.compile(r"/(?:market-board|deploy-cash)(?:/|$)")


def is_route_allowed(method: str, path: str) -> bool:
    """Whether an external-demo request may reach ``method path`` at all."""
    if method in _SAFE_METHODS:
        if path in _GLOBAL_READS:
            return True
        return bool(_HOUSEHOLD_PATH.match(path)) and not _ADVICE_SUFFIX.search(path)
    return method == "POST" and path in _POST_ALLOWED


# ── Invites and sessions ─────────────────────────────────────────────────────
def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    # Naive UTC, matching the other DateTime columns on both SQLite and Postgres.
    return datetime.now(UTC).replace(tzinfo=None)


def create_invite(session: Session) -> tuple[str, models.ExternalDemoInvite]:
    """Mint one single-use invite. Returns the plaintext code (shown once) and the row."""
    code = secrets.token_urlsafe(24)
    now = _now()
    invite = models.ExternalDemoInvite(
        id=uuid.uuid4(),
        code_digest=_digest(code),
        created_at=now,
        expires_at=now + timedelta(hours=settings.external_demo_invite_ttl_hours),
    )
    session.add(invite)
    session.commit()
    _count(session, "external_demo_invite_created", str(invite.id))
    return code, invite


def redeem_invite(session: Session, code: str) -> tuple[str, models.ExternalDemoSession] | None:
    """Redeem a code into a new session, or None when the code is unknown, used or
    expired. Single use is enforced by a conditional UPDATE, so two concurrent redemptions
    of one code cannot both succeed."""
    now = _now()
    invite_id = session.scalar(
        select(models.ExternalDemoInvite.id).where(models.ExternalDemoInvite.code_digest == _digest(code))
    )
    if invite_id is None:
        return None
    claimed = session.execute(
        update(models.ExternalDemoInvite)
        .where(
            models.ExternalDemoInvite.id == invite_id,
            models.ExternalDemoInvite.redeemed_at.is_(None),
            models.ExternalDemoInvite.expires_at > now,
        )
        .values(redeemed_at=now)
    )
    if claimed.rowcount != 1:
        session.rollback()
        return None
    token = secrets.token_urlsafe(32)
    row = models.ExternalDemoSession(
        id=uuid.uuid4(),
        token_digest=_digest(token),
        invite_id=invite_id,
        created_at=now,
        expires_at=now + timedelta(hours=settings.external_demo_session_ttl_hours),
    )
    session.add(row)
    session.commit()
    _count(session, "external_demo_session_started", str(row.id))
    return token, row


def resolve_session(session: Session, token: str) -> models.ExternalDemoSession | None:
    """The live session for ``token``, or None. Every session is dead while unreleased."""
    if not settings.external_demo_released or not token:
        return None
    row = session.scalar(
        select(models.ExternalDemoSession).where(models.ExternalDemoSession.token_digest == _digest(token))
    )
    if row is None or row.expires_at <= _now():
        return None
    return row


# ── Funnel counters (first-party, in the existing ``events`` sink) ───────────
_TAP_PREFIX = "external_demo_tap_"


def _count(session: Session, event_name: str, session_key: str) -> None:
    events_service.record_event(session, event_name=event_name, session_id=session_key)


def record_tap(session: Session, demo_session: models.ExternalDemoSession, card_key: str) -> None:
    """Count one tap on a locked card. ``card_key`` must already be a member of the closed
    card set; the event name is built from that set's own value."""
    key = next(k for k in sorted(ALL_CARD_KEYS) if k == card_key)
    _count(session, f"{_TAP_PREFIX}{key}", str(demo_session.id))


def counters(session: Session) -> dict:
    """Invites created, sessions started, and taps per locked card (all-time totals)."""
    from sqlalchemy import func

    rows = dict(
        session.execute(
            select(models.Event.event_name, func.count(models.Event.id))
            .where(models.Event.event_name.like("external_demo_%"))
            .group_by(models.Event.event_name)
        ).all()
    )
    return {
        "invites_created": int(rows.get("external_demo_invite_created", 0)),
        "sessions_started": int(rows.get("external_demo_session_started", 0)),
        "locked_card_taps": {k: int(rows.get(f"{_TAP_PREFIX}{k}", 0)) for k in sorted(ALL_CARD_KEYS)},
    }
