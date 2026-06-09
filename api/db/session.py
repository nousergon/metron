"""SQLAlchemy engine + session factory.

Dev uses SQLite (file or in-memory); production uses Postgres via the same URL
knob — no model changes required. The declarative ``Base`` lives here so every
model module shares one metadata.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from api.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def _engine_kwargs(url: str) -> dict:
    # SQLite needs check_same_thread off for the dev server's threadpool.
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_all() -> None:
    """Create tables for dev/test. Production uses migrations (Alembic) instead."""
    # Import models so they register on Base.metadata before create_all.
    from api.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
