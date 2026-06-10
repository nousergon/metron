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

from api.db.session import Base, _sync_additive_columns


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


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
