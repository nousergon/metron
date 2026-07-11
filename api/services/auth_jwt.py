"""Bearer-JWT verification against the shared nousergon-auth identity service.

metron-ops#179: Metron no longer owns login. The shared service
(https://github.com/nousergon/nousergon-auth, deployed at ``settings.auth_base_url``)
authenticates users and mints short-lived JWTs (better-auth ``jwt`` plugin:
EdDSA/Ed25519, 15-minute expiry, payload ``{sub: <identity user id>, email}``, ``iss``
and ``aud`` both equal to the service's base URL). This module verifies those tokens
locally against the service's published JWKS — cached by ``PyJWKClient``, so there is
no per-request round-trip to the identity service.

Mirrors the reference implementation in nousergon/vires ``api/services/auth_jwt.py``
(vires-PR97) — the two products verify the same contract the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from api.config import settings

# better-auth's jwt plugin signs with EdDSA (crv Ed25519) unless explicitly
# reconfigured — pinned, not negotiated, so a downgrade to a weaker alg in a
# forged token header is rejected outright.
ALGORITHMS = ["EdDSA"]


class IdentityTokenError(Exception):
    """The presented bearer token failed verification (bad signature, expired,
    wrong issuer/audience, malformed, or the JWKS could not be fetched)."""


@dataclass(frozen=True)
class IdentityClaims:
    """The verified identity the shared service vouches for."""

    sub: str  # nousergon-auth's stable user.id — what users.identity_user_id stores
    email: str | None


@lru_cache
def _jwk_client() -> PyJWKClient:
    return PyJWKClient(
        f"{settings.auth_base_url}/api/auth/jwks",
        cache_keys=True,
        lifespan=settings.auth_jwks_cache_seconds,
    )


def verify_identity_token(token: str) -> IdentityClaims:
    """Verify a nousergon-auth JWT and return its claims.

    Raises :class:`IdentityTokenError` on ANY failure — callers translate to 401.
    Verification is strict: signature (Ed25519 via JWKS), ``exp``, ``iss``, and
    ``aud`` are all enforced.
    """
    expected_audience = settings.auth_jwt_audience or settings.auth_base_url
    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            issuer=settings.auth_base_url,
            audience=expected_audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise IdentityTokenError(str(e)) from e

    email = claims.get("email")
    return IdentityClaims(
        sub=str(claims["sub"]),
        email=email.lower() if isinstance(email, str) else None,
    )
