"""Shared fixtures: an isolated in-memory SQLite per test, exposed both as a raw
``db_session`` (service-layer unit tests) and behind a ``client`` with the
``get_session`` dependency overridden (API tests).

StaticPool keeps ONE connection so the in-memory DB is shared across the TestClient's
worker thread — otherwise each thread would get its own empty database.
"""

from __future__ import annotations

import boto3
import botocore.exceptions
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.db.session import Base, get_session
from api.main import app
from api.services import compute_cache


@pytest.fixture(autouse=True)
def _clear_compute_cache():
    """The compute cache is a process-level global; clear it around every test so one
    test's cached result can never bleed into another's isolated in-memory DB."""
    compute_cache.clear()
    yield
    compute_cache.clear()


class _NoCredsStubClient:
    """Stands in for a real boto3 client: construction never fails (matching real
    botocore, which resolves credentials lazily at request-signing time), but any method
    call — get_object, list_objects_v2, whatever a future call site adds — raises, same
    as a real client would with no credentials/network reachable."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise botocore.exceptions.NoCredentialsError()

        return _raise


@pytest.fixture(autouse=True)
def _no_live_aws_calls(monkeypatch):
    """Tests must be hermetic w.r.t. AWS — never depend on ambient credentials/network.
    ~14 call sites across api/services/* and portfolio_analytics/*/spine_source.py each
    construct their own ``boto3.client("s3")`` ad hoc (no shared factory to patch
    instead) and treat any failure as fail-soft (empty/None), by design. Left unpatched,
    a machine with real AWS credentials (e.g. a developer's authenticated shell via
    ``~/.aws/credentials``) lets these silently reach REAL production S3 and pull REAL
    data into an otherwise-isolated in-memory test DB — assertions become
    environment-dependent (pass in creds-less CI, fail locally, or drift as the live
    artifact's content changes). Stub every ``boto3.client(...)`` call during tests so
    every call site falls through to its already-coded fail-soft path — the behavior an
    isolated unit test should see by default regardless of who runs it. Tests exercising
    real artifact-handling logic inject their own reader/``s3=`` callable (the seam every
    S3-backed function already exposes for this) — see ``test_reference_rate.py``'s
    ``reader=`` fixtures."""
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: _NoCredsStubClient())


@pytest.fixture()
def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(_engine):
    return sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db_session(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(session_factory):
    def _override():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
