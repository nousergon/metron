"""Every model table must be created by a migration (metron-ops#268).

`AGENTS.md` says "Alembic owns the schema. A model change without a migration will pass
tests locally and fail on deploy, because the test database is built from migrations."
The first half is the intent; the second half was not true. Measured 2026-08-03:

- Every test builds its DB from `Base.metadata.create_all` on in-memory SQLite
  (`conftest._engine`), so the model set is always self-consistent in tests.
- The "pytest on Postgres (dialect parity gate)" job runs `alembic upgrade head` against
  a fresh Postgres and then runs that same SQLite-backed suite — so it proves the
  migrations APPLY to an empty database and nothing more.
- `api.db.session.create_all` returns immediately on Postgres by design, so the models
  never self-heal there.

Net effect: `shadow_recompute_breaks` shipped as a model with no migration and simply did
not exist on Neon, undetected, until a diff of `Base.metadata` against the live inspector
turned it up while unrelated work was being deployed.

This is a static check on purpose. Executing the migrations against SQLite would trip on
Postgres-flavoured DDL (`server_default=sa.text('now()')`), and executing them against a
real Postgres is what the CI job already does — the gap was never "do they run", it was
"do they cover the models".
"""

from __future__ import annotations

import pathlib
import re

from api.db.models import Base

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"

_CREATE = re.compile(r"op\.create_table\(\s*['\"]([a-z_0-9]+)['\"]")
_DROP = re.compile(r"op\.drop_table\(\s*['\"]([a-z_0-9]+)['\"]")


def _tables_from_migrations() -> set[str]:
    """Tables the migration history creates, minus any it later drops.

    `upgrade()` and `downgrade()` are not separated: a `drop_table` in a downgrade body
    only ever undoes a create in the same revision, so the net set is unchanged, while a
    real removal migration nets out correctly. Coarser than parsing each function, and it
    cannot report a false MISSING — which is the only direction this test asserts.
    """
    created: set[str] = set()
    dropped: set[str] = set()
    for f in sorted(VERSIONS.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        created |= set(_CREATE.findall(text))
        dropped |= set(_DROP.findall(text))
    return created - (dropped - created)


def test_every_model_table_has_a_migration():
    missing = sorted(set(Base.metadata.tables) - _tables_from_migrations())
    assert not missing, (
        f"model tables with no migration: {missing}. These exist on SQLite (create_all) "
        "and NOT on Postgres, where create_all is a deliberate no-op — so they are absent "
        "in production until someone diffs the live schema. Add a revision under "
        "alembic/versions/."
    )


def test_the_check_can_see_the_migrations_at_all():
    """A guard that finds no migrations would pass the assertion above vacuously."""
    found = _tables_from_migrations()
    assert len(found) > 10, f"only {len(found)} tables parsed out of {VERSIONS} — the parser is broken"
