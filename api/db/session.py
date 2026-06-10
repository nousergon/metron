"""SQLAlchemy engine + session factory.

Dev uses SQLite (file or in-memory); production uses Postgres via the same URL
knob — no model changes required. The declarative ``Base`` lives here so every
model module shares one metadata.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import NullType

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


def _sync_additive_columns(bind: Engine) -> None:
    """Bring an EXISTING SQLite dev DB up to the model by adding missing **nullable**
    columns. ``create_all`` creates missing *tables* but never ALTERs an existing one,
    so a new column on an already-created table (e.g. ``securities.sector``) would be
    invisible to the personal-tier dev.sqlite without this. SQLite-only and additive
    by construction — Postgres (the multi-tenant tier) is managed by Alembic instead.

    Fail-loud: a model column that's missing AND non-nullable (with no server default)
    can't be back-filled safely on existing rows, so this raises rather than guessing —
    such a change needs a real migration, not an auto-ALTER."""
    if not bind.dialect.name == "sqlite":
        return  # Postgres → Alembic owns schema evolution; don't auto-ALTER.
    insp = inspect(bind)
    existing_tables = set(insp.get_table_names())
    with bind.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all just made it — already at the model
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.server_default is None:
                    raise RuntimeError(
                        f"Cannot auto-add non-nullable column {table.name}.{col.name} to an "
                        "existing SQLite DB — write a real migration."
                    )
                col_type = col.type.compile(dialect=bind.dialect)
                if isinstance(col.type, NullType):  # defensive — never expected
                    raise RuntimeError(f"Column {table.name}.{col.name} has no compilable type.")
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'))


def create_all() -> None:
    """Create tables for dev/test. Production uses migrations (Alembic) instead."""
    # Import models so they register on Base.metadata before create_all.
    from api.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _sync_additive_columns(engine)
