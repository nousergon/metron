"""A non-local deployment must not silently run on SQLite (metron-ops#264).

The dashbox carries two databases: every systemd unit gets Neon from a drop-in, while the
repo-root env file still names `sqlite:////home/ec2-user/metron/personal.sqlite`. Anything
run without that drop-in reads a file that stopped being written on 2026-07-25 — during
the metron-ops#260 investigation it answered a diagnostic query with confident, stale
data, and nothing looked wrong.

The guard runs at engine construction, which is import time for `api.db.session`, so
these tests call the predicate directly rather than re-importing the module.
"""

from __future__ import annotations

import pytest

from api.db.session import _assert_database_is_deliberate

SQLITE = "sqlite:////home/ec2-user/metron/personal.sqlite"
POSTGRES = "postgresql+psycopg://u:p@example.neon.tech/neondb"


@pytest.mark.parametrize("env", ["dev", "test", "local", "DEV", "Local"])
def test_sqlite_is_fine_locally(env):
    """README's self-host path ships ENV=dev with the SQLite default — it must keep
    working, case-insensitively."""
    _assert_database_is_deliberate(SQLITE, env, allow_sqlite=False)


@pytest.mark.parametrize("env", ["personal", "production", "staging"])
def test_sqlite_on_a_deployed_env_raises(env):
    """`personal` is the value live on the dashbox — the exact configuration that read
    four-day-stale rows without complaint."""
    with pytest.raises(RuntimeError, match="refusing to start"):
        _assert_database_is_deliberate(SQLITE, env, allow_sqlite=False)


def test_the_error_names_the_way_out():
    """A guard that blocks without saying how to proceed deliberately gets disabled by
    the next person who hits it."""
    with pytest.raises(RuntimeError, match="METRON_ALLOW_SQLITE"):
        _assert_database_is_deliberate(SQLITE, "personal", allow_sqlite=False)


def test_the_escape_hatch_works():
    """Someone genuinely running non-dev SQLite is not forbidden — only made explicit."""
    _assert_database_is_deliberate(SQLITE, "personal", allow_sqlite=True)


@pytest.mark.parametrize("env", ["dev", "personal", "production"])
def test_postgres_is_never_blocked(env):
    _assert_database_is_deliberate(POSTGRES, env, allow_sqlite=False)
