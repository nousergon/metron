"""Shared-identity bearer-JWT path + demo carve-out (metron-ops#179).

Uses ``raw_client`` (no ``require_tenant_id`` override) so the REAL verification path
runs end to end. Only the JWKS **fetch** is stubbed — signature checks (real Ed25519
keys), ``exp``/``iss``/``aud`` enforcement, the resolution ladder
(identity_user_id → email link → JIT-provision), and the demo ``X-Tenant-Id``
carve-out all execute for real. Mirrors nousergon/vires tests/test_auth_jwt.py
(vires-PR97) — same contract, same test approach.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from api.config import settings
from api.db import models
from api.services.demo import DEMO_TENANT_ID

ISSUER = "https://auth.nousergon.ai"  # matches Settings.auth_base_url default

_PRIVATE_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY = _PRIVATE_KEY.public_key()


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch):
    """Serve the test keypair's public half where the JWKS lookup would go —
    everything downstream of the key fetch is the real code path."""
    import api.services.auth_jwt as auth_jwt

    stub = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=_PUBLIC_KEY))
    monkeypatch.setattr(auth_jwt, "_jwk_client", lambda: stub)


def make_token(
    sub: str | None,
    email: str | None,
    *,
    issuer: str = ISSUER,
    audience: str = ISSUER,
    expires_in: int = 300,
    key: Ed25519PrivateKey | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    if sub is not None:
        claims["sub"] = sub
    if email is not None:
        claims["email"] = email
    return pyjwt.encode(claims, key or _PRIVATE_KEY, algorithm="EdDSA")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def list_portfolios(raw_client, headers: dict[str, str]):
    return raw_client.get("/portfolios", headers=headers)


def _seed_user(db, *, email: str, identity_user_id: str | None) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = models.Tenant(id=uuid.uuid4(), name=email)
    user = models.User(
        id=uuid.uuid4(), tenant_id=tenant.id, email=email, identity_user_id=identity_user_id
    )
    db.add(tenant)
    db.add(user)
    db.commit()
    return tenant.id, user.id


# ---- bearer path ------------------------------------------------------------ #


def test_no_credentials_rejected(raw_client):
    assert raw_client.get("/portfolios").status_code == 401


def test_resolves_existing_user_by_identity_user_id(raw_client, db_session):
    tenant_id, _ = _seed_user(db_session, email="linked@example.com", identity_user_id="idu-linked")
    db_session.add(models.Portfolio(id=uuid.uuid4(), tenant_id=tenant_id, name="Mine"))
    db_session.commit()

    r = list_portfolios(raw_client, bearer(make_token("idu-linked", "linked@example.com")))
    assert r.status_code == 200, r.text
    assert [p["name"] for p in r.json()] == ["Mine"]


def test_me_echoes_resolved_tenant(raw_client, db_session):
    """GET /me — the deploy-verification chokepoint: bearer in, resolved tenant out."""
    tenant_id, _ = _seed_user(db_session, email="who@example.com", identity_user_id="idu-who")
    r = raw_client.get("/me", headers=bearer(make_token("idu-who", "who@example.com")))
    assert r.status_code == 200, r.text
    assert r.json() == {"tenant_id": str(tenant_id)}


def test_me_demo_carve_out(raw_client):
    r = raw_client.get("/me", headers={"X-Tenant-Id": str(DEMO_TENANT_ID)})
    assert r.status_code == 200
    assert r.json() == {"tenant_id": str(DEMO_TENANT_ID)}


def test_me_unauthenticated_rejected(raw_client):
    assert raw_client.get("/me").status_code == 401


def test_links_unlinked_user_by_email_once_case_insensitive(raw_client, db_session):
    """The one-time email-link rung — makes the planned backfill self-healing. The JWT
    email claim arrives in whatever case the IdP stored, so matching is caseless."""
    tenant_id, _ = _seed_user(db_session, email="Legacy@Example.com", identity_user_id=None)

    r = list_portfolios(raw_client, bearer(make_token("idu-new", "legacy@example.com")))
    assert r.status_code == 200, r.text
    db_session.expire_all()
    linked = db_session.scalar(select(models.User).where(models.User.identity_user_id == "idu-new"))
    assert linked is not None
    assert linked.tenant_id == tenant_id


def test_email_linked_to_different_identity_conflicts(raw_client, db_session):
    _seed_user(db_session, email="taken@example.com", identity_user_id="idu-original")
    r = list_portfolios(raw_client, bearer(make_token("idu-usurper", "taken@example.com")))
    assert r.status_code == 409


def test_jit_provisions_new_tenant_and_user(raw_client, db_session):
    r = list_portfolios(raw_client, bearer(make_token("idu-brand-new", "new@example.com")))
    assert r.status_code == 200, r.text
    user = db_session.scalar(select(models.User).where(models.User.identity_user_id == "idu-brand-new"))
    assert user is not None
    assert user.email == "new@example.com"
    assert db_session.get(models.Tenant, user.tenant_id) is not None


def test_jit_provision_is_stable_across_requests(raw_client, db_session):
    """Second request with the same identity resolves to the SAME tenant — the exact
    anti-"always mint a new workspace" contract (the vires-ops#57 failure mode)."""
    tok = make_token("idu-stable", "stable@example.com")
    assert list_portfolios(raw_client, bearer(tok)).status_code == 200
    assert list_portfolios(raw_client, bearer(tok)).status_code == 200
    users = db_session.scalars(
        select(models.User).where(models.User.identity_user_id == "idu-stable")
    ).all()
    assert len(users) == 1


def test_unknown_identity_without_email_claim_rejected(raw_client):
    """users.email is non-nullable and names the JIT tenant — a token missing the email
    claim (a contract violation; better-auth always includes it) cannot provision."""
    r = list_portfolios(raw_client, bearer(make_token("idu-no-email", None)))
    assert r.status_code == 401


def test_expired_token_rejected(raw_client):
    tok = make_token("idu-x", "x@example.com", expires_in=-60)
    assert list_portfolios(raw_client, bearer(tok)).status_code == 401


def test_missing_sub_rejected(raw_client):
    tok = make_token(None, "x@example.com")
    assert list_portfolios(raw_client, bearer(tok)).status_code == 401


def test_wrong_issuer_rejected(raw_client):
    tok = make_token("idu-x", "x@example.com", issuer="https://evil.example.com")
    assert list_portfolios(raw_client, bearer(tok)).status_code == 401


def test_wrong_audience_rejected(raw_client):
    tok = make_token("idu-x", "x@example.com", audience="https://other.example.com")
    assert list_portfolios(raw_client, bearer(tok)).status_code == 401


def test_wrong_key_rejected(raw_client):
    tok = make_token("idu-x", "x@example.com", key=Ed25519PrivateKey.generate())
    assert list_portfolios(raw_client, bearer(tok)).status_code == 401


def test_garbage_token_rejected(raw_client):
    assert list_portfolios(raw_client, bearer("not-a-jwt")).status_code == 401


def test_non_bearer_scheme_rejected(raw_client):
    assert list_portfolios(raw_client, {"Authorization": "Basic dXNlcjpwdw=="}).status_code == 401


# ---- demo carve-out (X-Tenant-Id) ------------------------------------------ #


def test_demo_tenant_header_accepted(raw_client):
    r = list_portfolios(raw_client, {"X-Tenant-Id": str(DEMO_TENANT_ID)})
    assert r.status_code == 200, r.text


def test_demo_header_rejected_when_demo_disabled(raw_client, monkeypatch):
    monkeypatch.setattr(settings, "demo_enabled", False)
    r = list_portfolios(raw_client, {"X-Tenant-Id": str(DEMO_TENANT_ID)})
    assert r.status_code == 401


def test_arbitrary_tenant_header_rejected(raw_client, db_session):
    """The blind-trust hole this cutover closes: a real tenant's id in X-Tenant-Id no
    longer grants access to that tenant's data."""
    tenant_id, _ = _seed_user(db_session, email="victim@example.com", identity_user_id="idu-victim")
    r = list_portfolios(raw_client, {"X-Tenant-Id": str(tenant_id)})
    assert r.status_code == 401


def test_malformed_tenant_header_rejected(raw_client):
    assert list_portfolios(raw_client, {"X-Tenant-Id": "not-a-uuid"}).status_code == 401


def test_invalid_bearer_does_not_fall_back_to_demo_header(raw_client):
    """A presented-but-broken token must 401 even when an acceptable demo X-Tenant-Id
    rides along — the bearer is authoritative, never a silent fallthrough."""
    headers = {**bearer("broken"), "X-Tenant-Id": str(DEMO_TENANT_ID)}
    assert list_portfolios(raw_client, headers).status_code == 401
