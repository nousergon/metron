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
from sqlalchemy.schema import CreateTable
from sqlalchemy.types import NullType

from api.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def _engine_kwargs(url: str) -> dict:
    # SQLite needs check_same_thread off for the dev server's threadpool.
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


# Environments where a SQLite database is the expected, documented setup: local dev and
# the test suite. README's self-host path ships `ENV=dev` with the SQLite default, so a
# self-hoster following the docs is unaffected by the guard below.
_SQLITE_OK_ENVS = frozenset({"dev", "test", "local"})


def _assert_database_is_deliberate(url: str, env: str, allow_sqlite: bool) -> None:
    """Refuse to hand out a SQLite engine to a non-local deployment (metron-ops#264).

    The dashbox carries TWO databases. Every systemd unit gets Neon from a drop-in, but
    the repo-root env file still names ``sqlite:////home/ec2-user/metron/personal.sqlite``
    — so anything run WITHOUT that drop-in (a manual ``python -m api.maintenance``, an
    ad-hoc query, a debugging session) silently reads a file that stopped being written
    on 2026-07-25. During the metron-ops#260 investigation it answered a diagnostic query
    with confident, four-days-stale data, and nothing about it looked wrong.

    A second database that silently answers is worse than none: nothing errors, the
    numbers merely disagree, and there is no signal about which one you are looking at.
    So a non-local ``ENV`` plus a SQLite URL raises here, at engine construction, instead
    of quietly serving stale rows.

    ``METRON_ALLOW_SQLITE=true`` is the deliberate escape hatch for someone genuinely
    running a non-dev SQLite deployment — the point is to make the choice explicit, not
    to forbid it.
    """
    if not url.startswith("sqlite"):
        return
    if env.lower() in _SQLITE_OK_ENVS or allow_sqlite:
        return
    raise RuntimeError(
        f"refusing to start: DATABASE_URL is SQLite ({url!r}) but ENV={env!r} is not a "
        f"local environment ({sorted(_SQLITE_OK_ENVS)}). On a deployed box this almost "
        "always means the process is missing the environment that names the real "
        "database, and it would otherwise read a stale local file with no error "
        "(metron-ops#264). Point DATABASE_URL at the real database, or set "
        "METRON_ALLOW_SQLITE=true if this deployment genuinely runs on SQLite."
    )


_assert_database_is_deliberate(settings.database_url, settings.env, settings.allow_sqlite)
engine = create_engine(settings.database_url, **_engine_kwargs(settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


#: ``Session.info`` flag marking a session as scoped to ONE API request (short-lived, read
#: mostly). Per-request memoization keys off it — ``compute_cache.portfolio_fingerprint``
#: computes the portfolio's content fingerprint once per request instead of once per cached
#: call. Long-lived sessions (maintenance jobs) never carry it.
REQUEST_SCOPED = "metron.request_scoped"


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    session.info[REQUEST_SCOPED] = True
    try:
        yield session
    finally:
        session.close()


def _sync_missing_indexes(conn, table) -> None:
    """Create any index the model declares on ``table`` (single-column ``index=True``/
    ``unique=True`` and multi-column ``Index(...)`` alike — anything that lands in
    ``table.indexes``) that the live DB doesn't have yet. ``ALTER TABLE ADD COLUMN``
    carries no index/unique DDL, so a column added by ``_sync_additive_columns`` above
    never gets the index its model declaration promises — a uniqueness invariant that
    exists only in the model is silently unenforced in prod until someone notices.

    Fail-loud, same policy as the column sync: if a UNIQUE index can't be created
    because existing rows already violate it, that's a real data problem and the
    resulting ``IntegrityError`` propagates rather than being swallowed."""
    have_indexes = {ix["name"] for ix in inspect(conn).get_indexes(table.name)}
    for index in table.indexes:
        if index.name in have_indexes:
            continue
        cols = ", ".join(f'"{c.name}"' for c in index.columns)
        unique_sql = "UNIQUE " if index.unique else ""
        conn.execute(text(f'CREATE {unique_sql}INDEX IF NOT EXISTS "{index.name}" ON "{table.name}" ({cols})'))


def _migrate_sqlite_account_identity(bind: Engine) -> None:
    """Rebuild an EXISTING SQLite ``accounts`` table that predates ``identity_scope``
    (metron-ops#383), the one non-additive change the auto-ALTER cannot make.

    The old table carries ``UNIQUE (tenant_id, broker, external_id)`` as a table
    constraint, which SQLite cannot drop in place, and the new column is NOT NULL with a
    value derived per row. So this runs SQLite's documented table rebuild with foreign
    keys off: create the table at the model, copy every row with its scope (the portfolio
    id's hex for CSV/manual, ``tenant`` otherwise, the same rule the Postgres migration
    ``b4e9d2c6f813`` applies), drop the old table and rename. Row ids are copied, so
    every foreign key into ``accounts`` still resolves. No row is merged, split or moved.
    A no-op once the column exists, and on a fresh DB that ``create_all`` just built."""
    from api.db import models

    insp = inspect(bind)
    if "accounts" not in insp.get_table_names():
        return
    have = [c["name"] for c in insp.get_columns("accounts")]
    if "identity_scope" in have:
        return
    table = models.Account.__table__
    copied = [c.name for c in table.columns if c.name in have]
    labels = ", ".join(f"'{b}'" for b in sorted(models.PORTFOLIO_SCOPED_SOURCES))
    scope = f"CASE WHEN broker IN ({labels}) THEN portfolio_id ELSE '{models.TENANT_SCOPE}' END"
    ddl = str(CreateTable(table).compile(dialect=bind.dialect)).replace(
        'CREATE TABLE accounts', 'CREATE TABLE accounts__rebuild', 1
    )
    cols = ", ".join(f'"{c}"' for c in copied)
    with bind.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()  # the pragma is a no-op inside a transaction, so end the autobegun one
        try:
            with conn.begin():
                conn.exec_driver_sql(ddl)
                conn.exec_driver_sql(
                    f'INSERT INTO accounts__rebuild ({cols}, "identity_scope") '
                    f"SELECT {cols}, {scope} FROM accounts"
                )
                conn.exec_driver_sql("DROP TABLE accounts")
                conn.exec_driver_sql("ALTER TABLE accounts__rebuild RENAME TO accounts")
                _sync_missing_indexes(conn, table)
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()


def _sync_additive_columns(bind: Engine) -> None:
    """Bring an EXISTING SQLite dev DB up to the model by adding missing **nullable**
    columns, then any index the model declares that the live DB is still missing.
    ``create_all`` creates missing *tables* but never ALTERs an existing one, so a new
    column on an already-created table (e.g. ``securities.sector``) — or an index on
    one, like ``users.identity_user_id``'s unique index — would be invisible to the
    personal-tier dev.sqlite without this. SQLite-only and additive by construction —
    Postgres (the multi-tenant tier) is managed by Alembic instead.

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
            _sync_missing_indexes(conn, table)


def create_all(bind: Engine | None = None) -> None:
    """Create tables for the SQLite/personal tier. Postgres (multi-tenant) uses
    Alembic migrations — this gate is on DB dialect, not an ENV string, so a
    SQLite deploy always self-heals regardless of what ``ENV`` says (metron-ops#202)."""
    bind = bind or engine
    if not bind.dialect.name == "sqlite":
        return  # Postgres → Alembic owns schema evolution; never auto-DDL here.
    # Import models so they register on Base.metadata before create_all.
    from api.db import models  # noqa: F401

    Base.metadata.create_all(bind=bind)
    _migrate_sqlite_account_identity(bind)
    _sync_additive_columns(bind)
