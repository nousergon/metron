"""Shared fixtures: an isolated in-memory SQLite per test, exposed both as a raw
``db_session`` (service-layer unit tests) and behind a ``client`` with the
``get_session`` dependency overridden (API tests).

StaticPool keeps ONE connection so the in-memory DB is shared across the TestClient's
worker thread — otherwise each thread would get its own empty database.
"""

from __future__ import annotations

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
