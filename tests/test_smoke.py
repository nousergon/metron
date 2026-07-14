"""PH0 smoke tests — the skeleton boots, the engine is wired in, and the schema
round-trips on SQLite. Uses an isolated in-memory DB per the test override.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import _test_tenant_id
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.db import models
from api.db.session import Base, get_session
from api.main import app
from api.services import identity


@pytest.fixture()
def client():
    # Isolated in-memory SQLite for the test, injected via the get_session override.
    # StaticPool keeps ONE connection so the in-memory DB is shared across the
    # TestClient's worker thread (otherwise each thread gets its own empty DB).
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    # Same auth seam swap as the shared conftest `client` fixture: this file exercises
    # tenant ISOLATION, not authentication — the real bearer-JWT path is covered end to
    # end in tests/test_auth_jwt.py.
    app.dependency_overrides[identity.require_tenant_id] = _test_tenant_id
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_meta_reports_engine_and_posture(client):
    r = client.get("/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "portfolio-analytics"
    assert body["engine_version"]  # non-empty
    assert "risk" in body["capabilities"]
    # The trust posture is part of the product contract.
    assert body["posture"] == {"ai": False, "ads_or_trackers": False, "advice": False, "read_only": True}


def test_portfolio_requires_tenant(client):
    assert client.get("/portfolios").status_code == 401


def test_portfolio_roundtrips_per_tenant(client):
    tenant_a = str(uuid.uuid4())
    tenant_b = str(uuid.uuid4())

    created = client.post("/portfolios", json={"name": "Taxable"}, headers={"X-Tenant-Id": tenant_a})
    assert created.status_code == 201
    assert created.json()["name"] == "Taxable"
    assert created.json()["base_currency"] == "USD"

    # Tenant A sees its portfolio; tenant B sees none (isolation by tenant_id).
    assert len(client.get("/portfolios", headers={"X-Tenant-Id": tenant_a}).json()) == 1
    assert client.get("/portfolios", headers={"X-Tenant-Id": tenant_b}).json() == []


def test_schema_has_core_tables():
    # The plan's required grain: tenant, portfolio, transaction, position, price (+ user/account/security).
    tables = set(Base.metadata.tables)
    assert {
        "tenants",
        "users",
        "portfolios",
        "accounts",
        "securities",
        "transactions",
        "positions",
        "price_bars",
    } <= tables


def test_models_align_with_engine_txn_types():
    # The transactions table stores TxnType.value strings the engine emits.
    from portfolio_analytics.domain.ledger import TxnType

    assert hasattr(models.Transaction, "txn_type")
    assert TxnType.BUY.value  # engine enum reachable from the app
