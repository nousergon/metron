"""Authenticated tenant resolution — the PH4 auth layer (metron-ops#179).

This replaces the pre-auth ``X-Tenant-Id`` trust stub that lived in
``api/routers/portfolios.py::_tenant_id`` (and its mirror in metron-ops
``metron_ext/deps.py``): any client could previously claim any tenant by sending an
arbitrary UUID header. Now:

- **Bearer JWT (the auth path):** ``Authorization: Bearer <jwt>`` carries a
  short-lived token minted by the shared nousergon-auth identity service, verified
  locally against its JWKS (see ``api.services.auth_jwt``). The verified ``sub``
  claim resolves to a local ``User`` via the ``users.identity_user_id`` column —
  matched by id first, then linked once by email, else JIT-provisioned with a fresh
  personal ``Tenant`` (the same one-workspace-per-user model the retired in-process
  Better Auth ``databaseHooks`` implemented in web/lib/auth.ts).

- **Demo carve-out (the ONLY surviving ``X-Tenant-Id`` acceptance):** the public
  read-only demo (`/demo`, metron-ops#42) has no auth session, so the web tier still
  sends ``X-Tenant-Id`` for it — accepted ONLY when the value is exactly the fixed
  demo tenant id AND this deployment seeds the demo (``settings.demo_enabled``).
  Writes to the demo tenant stay refused by the ``_demo_read_only`` middleware in
  ``api/main.py``. Any other ``X-Tenant-Id`` value is a 401, closing the blind-trust
  hole while keeping the signup-free demo working.

A presented bearer token is authoritative: it either verifies or the request is 401 —
an invalid Bearer NEVER falls through to the ``X-Tenant-Id`` path.

Mirrors the resolution-ladder contract in nousergon/vires
``api/db/identity.py::_resolve_identity_user`` (vires-PR97).
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.db.session import get_session
from api.services.auth_jwt import IdentityTokenError, verify_identity_token
from api.services.demo import DEMO_TENANT_ID


def resolve_identity_user(session: Session, sub: str, email: str | None) -> models.User:
    """Map a verified nousergon-auth identity to a local ``User`` row.

    Resolution ladder (metron-ops#179, mirroring vires-PR97): match by
    ``identity_user_id``; else link once by email (only onto a row not yet linked to a
    DIFFERENT identity — this also makes the planned one-time backfill of the single
    pre-cutover user self-healing); else JIT-provision a fresh personal ``Tenant`` +
    ``User``. This is the contract that replaces "always mint a new workspace" — the
    data-orphaning failure mode the Vires cutover hit (vires-ops#57).
    """
    user = session.scalar(select(models.User).where(models.User.identity_user_id == sub))
    if user is not None:
        return user

    if email:
        by_email = session.scalar(
            select(models.User).where(func.lower(models.User.email) == email.lower())
        )
        if by_email is not None:
            if by_email.identity_user_id is not None:
                # Same email, different identity id: the shared service's account for
                # this address was recreated out from under the link. Silently
                # re-linking would hand the new identity the old identity's data —
                # refuse loudly instead; resolving this is an explicit operator action.
                raise HTTPException(
                    status_code=409,
                    detail="This email is already linked to a different identity — "
                    "contact the administrator.",
                )
            by_email.identity_user_id = sub
            session.commit()
            return by_email

    if not email:
        # ``users.email`` is non-nullable and the JIT tenant is named after it; a
        # token without an email claim can't provision a workspace. better-auth
        # always includes ``email`` in the JWT payload, so this is a contract
        # violation, not an expected state — fail loud.
        raise HTTPException(status_code=401, detail="Identity token is missing the email claim")

    tenant = models.Tenant(id=uuid.uuid4(), name=email)
    user = models.User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=email,
        identity_user_id=sub,
    )
    session.add(tenant)
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        # Two concurrent first requests for the same brand-new identity both reached
        # the JIT-provision branch; the unique index on identity_user_id (or email)
        # let exactly one win. Recover by re-reading the winner — anything still
        # missing after that is a real invariant violation and re-raises.
        session.rollback()
        user = session.scalar(select(models.User).where(models.User.identity_user_id == sub))
        if user is None:
            raise
    return user


def require_tenant_id(
    session: Session = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
) -> uuid.UUID:
    """FastAPI dependency: the authenticated caller's tenant id.

    Bearer JWT is the primary (and for real users, only) path; ``X-Tenant-Id`` is
    accepted solely for the read-only demo tenant (see module docstring). Everything
    else is 401.
    """
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail="Unsupported Authorization scheme")
        try:
            claims = verify_identity_token(token.strip())
        except IdentityTokenError as e:
            raise HTTPException(status_code=401, detail="Invalid or expired token") from e
        return resolve_identity_user(session, claims.sub, claims.email).tenant_id

    if x_tenant_id is not None:
        if settings.demo_enabled and x_tenant_id == str(DEMO_TENANT_ID):
            return DEMO_TENANT_ID
        raise HTTPException(
            status_code=401,
            detail="X-Tenant-Id is only honored for the read-only demo tenant — "
            "authenticate with a bearer token.",
        )

    raise HTTPException(status_code=401, detail="Not authenticated")
