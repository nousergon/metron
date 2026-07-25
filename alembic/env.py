"""Alembic migrations for Metron (SQLAlchemy + Postgres).

Reads DATABASE_URL from the environment — no hardcoded URL in alembic.ini.
For autogenerate, point at a running Postgres and run::

    DATABASE_URL=postgresql+psycopg://... alembic revision --autogenerate -m "description"

Apply pending migrations::

    alembic upgrade head
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic Config object
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ----------------------------------------------------------------
# Metron models — import everything so Base.metadata is populated
# ----------------------------------------------------------------
import sys, os

# Ensure the project root (parent of api/) is on sys.path so imports work
# regardless of CWD — the worktree root, not the shared checkout.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from api.db.session import Base  # noqa: E402
import api.db.models  # noqa: E402 — registers all tables on Base.metadata

# metron-ops overlay models (AdvisorProfile, AdvisorCommentary) —
# optional, only present when metron-ops is installed alongside metron.
try:
    import metron_ext.advisor.models  # noqa: F401
except ImportError:
    pass

target_metadata = Base.metadata

# ----------------------------------------------------------------
# Database URL — from the environment, not alembic.ini
# ----------------------------------------------------------------
_DATABASE_URL = os.environ.get("DATABASE_URL")
if _DATABASE_URL:
    config.set_main_option("sqlalchemy.url", _DATABASE_URL)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
