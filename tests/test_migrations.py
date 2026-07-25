"""The additive-column shim that keeps an EXISTING personal-tier SQLite DB in sync.

``create_all`` creates missing *tables* but never ALTERs an existing one, so a new
nullable column on an already-created table would be invisible to a persisted
dev.sqlite. ``_sync_additive_columns`` closes that gap (additively, SQLite-only).
These exercise the ALTER path the in-memory test DBs don't reach (they're built fresh
with every column present).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from api.db.session import Base, _sync_additive_columns, create_all


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _index_names(engine, table: str) -> set[str]:
    return {ix["name"] for ix in inspect(engine).get_indexes(table)}


def test_missing_nullable_column_is_added(tmp_path):
    """An old DB missing a later nullable column (securities.sector) gets it back."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE securities DROP COLUMN sector"))  # simulate the old schema
    assert "sector" not in _columns(engine, "securities")

    _sync_additive_columns(engine)
    assert "sector" in _columns(engine, "securities")

    _sync_additive_columns(engine)  # idempotent — a second run is a no-op, no duplicate-column error
    assert "sector" in _columns(engine, "securities")
    engine.dispose()


def test_account_nickname_column_is_added(tmp_path):
    """The new nullable ``accounts.nickname`` back-fills onto an existing DB (the personal
    SQLite tier path — no Alembic needed)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE accounts DROP COLUMN nickname"))  # simulate the pre-nickname schema
    assert "nickname" not in _columns(engine, "accounts")

    _sync_additive_columns(engine)
    assert "nickname" in _columns(engine, "accounts")
    engine.dispose()


def test_account_cash_balance_column_is_added(tmp_path):
    """The new nullable ``accounts.cash_balance_usd`` back-fills onto an existing DB
    (same personal-SQLite-tier path as ``nickname`` — no Alembic needed). This is the
    column that fixed the connector-cash-dropped-at-persistence bug (metron-ops)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE accounts DROP COLUMN cash_balance_usd"))  # simulate the pre-fix schema
    assert "cash_balance_usd" not in _columns(engine, "accounts")

    _sync_additive_columns(engine)
    assert "cash_balance_usd" in _columns(engine, "accounts")
    engine.dispose()


def test_account_nav_snapshots_table_is_created(tmp_path):
    """The new ``account_nav_snapshots`` table auto-creates via create_all (checkfirst) on
    an older DB that predates it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE account_nav_snapshots"))  # simulate the older schema
    assert "account_nav_snapshots" not in set(inspect(engine).get_table_names())

    Base.metadata.create_all(bind=engine)  # checkfirst recreates only the missing table
    assert "account_nav_snapshots" in set(inspect(engine).get_table_names())
    engine.dispose()


def test_missing_unique_index_is_created(tmp_path):
    """An old DB missing ``users.identity_user_id`` (and therefore its unique index)
    gets both back. ``ALTER TABLE ADD COLUMN`` carries no index/unique DDL, so the
    column sync alone would leave the model's uniqueness invariant unenforced."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        # SQLite refuses to drop a column an index still depends on — drop the index first.
        conn.execute(text("DROP INDEX ix_users_identity_user_id"))
        conn.execute(text("ALTER TABLE users DROP COLUMN identity_user_id"))  # simulate the pre-cutover schema
    assert "identity_user_id" not in _columns(engine, "users")
    assert "ix_users_identity_user_id" not in _index_names(engine, "users")

    _sync_additive_columns(engine)
    assert "identity_user_id" in _columns(engine, "users")
    indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("users")}
    identity_index = indexes["ix_users_identity_user_id"]
    assert identity_index["unique"]
    assert identity_index["column_names"] == ["identity_user_id"]

    _sync_additive_columns(engine)  # idempotent — no duplicate-index error
    engine.dispose()


def test_unique_index_enforces_uniqueness_after_sync(tmp_path):
    """The auto-created unique index is a real DB-level constraint, not cosmetic."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_users_identity_user_id"))
        conn.execute(text("ALTER TABLE users DROP COLUMN identity_user_id"))
    _sync_additive_columns(engine)

    insert = text(
        "INSERT INTO users (id, tenant_id, email, identity_user_id, created_at) "
        "VALUES (:id, :tenant_id, :email, :identity_user_id, CURRENT_TIMESTAMP)"
    )
    with engine.begin() as conn:
        conn.execute(insert, {"id": "a" * 32, "tenant_id": "t" * 32, "email": "a@example.com", "identity_user_id": "dup"})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                insert, {"id": "b" * 32, "tenant_id": "t" * 32, "email": "b@example.com", "identity_user_id": "dup"}
            )
    engine.dispose()


def test_missing_unique_index_over_dirty_data_fails_loud(tmp_path):
    """If existing rows already violate the model's declared uniqueness, creating the
    index can't proceed silently — that's a real data problem, not something to skip."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_users_identity_user_id"))  # column stays, index goes
        insert = text(
            "INSERT INTO users (id, tenant_id, email, identity_user_id, created_at) "
            "VALUES (:id, :tenant_id, :email, :identity_user_id, CURRENT_TIMESTAMP)"
        )
        conn.execute(insert, {"id": "a" * 32, "tenant_id": "t" * 32, "email": "a@example.com", "identity_user_id": "dup"})
        conn.execute(insert, {"id": "b" * 32, "tenant_id": "t" * 32, "email": "b@example.com", "identity_user_id": "dup"})
    assert "ix_users_identity_user_id" not in _index_names(engine, "users")

    with pytest.raises(IntegrityError):
        _sync_additive_columns(engine)
    engine.dispose()


def test_missing_non_nullable_column_fails_loud(tmp_path):
    """A non-nullable missing column can't be back-filled on existing rows safely — the
    shim raises (it needs a real migration) rather than guessing a value."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    # A pre-existing ``securities`` with only id+symbol → ``currency`` (NOT NULL, no
    # server default) is among the missing columns. create_all leaves this table as-is
    # (checkfirst) and creates the rest; the shim then hits the non-nullable gap.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE securities (id CHAR(32) NOT NULL PRIMARY KEY, symbol VARCHAR(40))"))
    Base.metadata.create_all(bind=engine)

    with pytest.raises(RuntimeError, match="non-nullable"):
        _sync_additive_columns(engine)
    engine.dispose()


@pytest.mark.parametrize("env_val", ["prod", "personal", "dev", "staging", ""])
def test_create_all_fires_on_sqlite_regardless_of_env(tmp_path, env_val, monkeypatch):
    """The self-heal fires on a SQLite engine regardless of what ``ENV`` says — only
    DB dialect (not deployment naming convention) should gate the auto-DDL path
    (metron-ops#202)."""
    monkeypatch.setattr("api.config.settings.env", env_val)
    engine = create_engine(f"sqlite:///{tmp_path / f'regression_{env_val}.sqlite'}")
    # Bootstrap then drop the sector column to simulate an old schema.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE securities DROP COLUMN sector"))
    assert "sector" not in _columns(engine, "securities")

    create_all(bind=engine)
    assert "sector" in _columns(engine, "securities")
    engine.dispose()


def test_create_all_skipped_for_non_sqlite_dialect():
    """A non-SQLite dialect never fires table creation or column sync — Postgres/
    multi-tenant tier uses Alembic exclusively (metron-ops#202)."""
    from unittest.mock import MagicMock

    mock_engine = MagicMock()
    mock_engine.dialect.name = "postgresql"

    # Should be a no-op: no tables created, no columns synced, no error raised.
    result = create_all(bind=mock_engine)
    assert result is None
    # Base.metadata.create_all was never called on the mock engine.
    mock_engine.connect.assert_not_called()
