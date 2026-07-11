"""Who-am-I — the authenticated caller's resolved workspace (metron-ops#179).

The one-call verification chokepoint for the shared-identity cutover: a bearer JWT
minted by nousergon-auth (or the demo X-Tenant-Id carve-out) resolves through
``api.services.identity.require_tenant_id`` to a tenant id, and this endpoint echoes
it. Deploy verification asserts the returned tenant matches the workspace that owns
the pre-cutover portfolios; the web tier can use it wherever it needs the raw tenant
id rather than just an Authorization header.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.services.identity import require_tenant_id

router = APIRouter(tags=["identity"])


class IdentityOut(BaseModel):
    tenant_id: uuid.UUID


@router.get("/me", response_model=IdentityOut)
def whoami(tenant_id: uuid.UUID = Depends(require_tenant_id)) -> IdentityOut:
    return IdentityOut(tenant_id=tenant_id)
