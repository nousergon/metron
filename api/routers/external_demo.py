"""External user demo endpoints (metron-ops-I310). Design: ``api/services/external_demo.py``.

- ``POST   /external-demo/invites`` — owner only; 403 while ``EXTERNAL_DEMO_RELEASED`` is off.
- ``GET    /external-demo/invites`` — owner-only list (no codes), with a live-session count
  per invite. Always available to the owner, released or not — it's the admin's own
  management view, not a demo-visitor surface.
- ``DELETE /external-demo/invites/{id}`` — owner-only revoke: deletes the invite and any
  session it minted, redeemed or not. Always available, like the list.
- ``POST /external-demo/sessions`` — redeem a single-use code; 403 while unreleased.
- ``GET  /external-demo/locked-cards`` — the locked cards (authenticated callers).
- ``POST /external-demo/taps`` — count a tap on a locked card (live external sessions only).
- ``GET  /external-demo/counters`` — owner-only funnel counters.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.db.session import get_session
from api.services import external_demo as svc
from api.services.auth_jwt import IdentityTokenError, verify_identity_token
from api.services.demo_household import DEMO_HOUSEHOLD_PORTFOLIO_ID
from api.services.identity import require_tenant_id

router = APIRouter(prefix="/external-demo", tags=["external-demo"])


def _require_released() -> None:
    if not settings.external_demo_released:
        raise HTTPException(status_code=403, detail="The external demo is not released.")


def _require_owner(authorization: str | None = Header(default=None)) -> None:
    """A verified identity whose email is listed in ``EXTERNAL_DEMO_ADMIN_EMAILS``.
    Empty list → nobody (fail closed)."""
    if authorization is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Unsupported Authorization scheme")
    try:
        claims = verify_identity_token(token.strip())
    except IdentityTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from e
    if not claims.email or claims.email not in settings.external_demo_admin_email_set:
        raise HTTPException(status_code=403, detail="Owner only.")


def _require_demo_session(
    session: Session = Depends(get_session),
    x_demo_session: str | None = Header(default=None),
) -> models.ExternalDemoSession:
    row = svc.resolve_session(session, (x_demo_session or "").strip())
    if row is None:
        raise HTTPException(status_code=401, detail="No live external-demo session.")
    return row


class InviteOut(BaseModel):
    code: str
    expires_at: datetime


class InviteListOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    redeemed_at: datetime | None
    live_session_count: int


class RedeemIn(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class SessionOut(BaseModel):
    token: str
    expires_at: datetime
    portfolio_id: uuid.UUID


class LockedCardOut(BaseModel):
    key: str
    name: str
    line: str
    status: str


class TapIn(BaseModel):
    card: str = Field(min_length=1, max_length=40)


@router.post("/invites", response_model=InviteOut, status_code=201)
def create_invite(
    _released: None = Depends(_require_released),
    _owner: None = Depends(_require_owner),
    session: Session = Depends(get_session),
) -> InviteOut:
    code, invite = svc.create_invite(session)
    return InviteOut(code=code, expires_at=invite.expires_at)


@router.get("/invites", response_model=list[InviteListOut])
def list_invites(
    _owner: None = Depends(_require_owner),
    session: Session = Depends(get_session),
) -> list[InviteListOut]:
    return [
        InviteListOut(
            id=invite.id,
            created_at=invite.created_at,
            expires_at=invite.expires_at,
            redeemed_at=invite.redeemed_at,
            live_session_count=count,
        )
        for invite, count in svc.list_invites(session)
    ]


@router.delete("/invites/{invite_id}", status_code=204)
def revoke_invite(
    invite_id: uuid.UUID,
    _owner: None = Depends(_require_owner),
    session: Session = Depends(get_session),
) -> Response:
    if not svc.revoke_invite(session, invite_id):
        raise HTTPException(status_code=404, detail="Invite not found.")
    return Response(status_code=204)


@router.post("/sessions", response_model=SessionOut, status_code=201)
def redeem(
    body: RedeemIn,
    _released: None = Depends(_require_released),
    session: Session = Depends(get_session),
) -> SessionOut:
    result = svc.redeem_invite(session, body.code.strip())
    if result is None:
        # One message for unknown, used and expired codes: no oracle for which.
        raise HTTPException(status_code=403, detail="This invite is not valid.")
    token, row = result
    return SessionOut(token=token, expires_at=row.expires_at, portfolio_id=DEMO_HOUSEHOLD_PORTFOLIO_ID)


@router.get("/locked-cards", response_model=list[LockedCardOut])
def get_locked_cards(_tenant: uuid.UUID = Depends(require_tenant_id)) -> list[LockedCardOut]:
    return [LockedCardOut(key=c.key, name=c.name, line=c.line, status=c.status) for c in svc.locked_cards()]


@router.post("/taps", status_code=204)
def tap(
    body: TapIn,
    demo_session: models.ExternalDemoSession = Depends(_require_demo_session),
    session: Session = Depends(get_session),
) -> Response:
    if body.card not in {c.key for c in svc.locked_cards()}:
        raise HTTPException(status_code=422, detail="Unknown card.")
    svc.record_tap(session, demo_session, body.card)
    return Response(status_code=204)


@router.get("/counters")
def get_counters(
    _owner: None = Depends(_require_owner),
    session: Session = Depends(get_session),
) -> dict:
    # Owner-admin surface (metron-ops-I323): the Settings invites section needs to know
    # the release state to disable "Create invite" and show why, without a second route —
    # this endpoint is already owner-only and already the section's one required fetch.
    return {**svc.counters(session), "external_demo_released": settings.external_demo_released}
