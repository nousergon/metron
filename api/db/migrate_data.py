#!/usr/bin/env python3
"""One-time SQLite → Postgres data migration for Metron.

Reads every row from a SQLite database and inserts into Postgres using
SQLAlchemy Core in FK-safe dependency order. SQLAlchemy's type system
handles all cross-dialect conversions (CHAR(32) UUID → native UUID,
TEXT JSON → JSONB, date strings → DATE/TIMESTAMPTZ).

Usage::

    SQLITE_URL=sqlite:////home/ec2-user/metron/personal.sqlite \\
    PG_URL=postgresql+psycopg://user:pass@host/db \\
    python -m api.db.migrate_data

The script is idempotent: truncate Postgres and re-run if needed.
Exits 0 on success, non-zero on any error.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, insert, inspect, select, text
from sqlalchemy.engine import Engine

# FK-safe dependency order: parents before children.
# Tables NOT in this list (e.g. metron-ops overlay tables) are skipped.
TABLE_ORDER = [
    "tenants",
    "users",
    "portfolios",
    "accounts",
    "securities",
    "transactions",
    "positions",
    "price_bars",
    "nav_snapshots",
    "account_nav_snapshots",
    "intraday_leg_snapshots",
    "realized_lots",
    "open_lots",
    "fx_rates",
    "investor_preferences",
    "watchlist_items",
    "security_labels",
    "security_classifications",
    "wallet_addresses",
    "crypto_value_snapshots",
    "reconciliation_breaks",
    "events",
    # metron-ops overlay tables — safe to include; skipped if not present
    "advisor_profiles",
    "advisor_commentary",
]


def _engine(url: str) -> Engine:
    return create_engine(url)


def _all_table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def migrate(*, sqlite_url: str, pg_url: str) -> None:
    src = _engine(sqlite_url)
    dst = _engine(pg_url)

    src_tables = _all_table_names(src)
    dst_tables = _all_table_names(dst)

    # Pre-flight: verify every source table has a matching destination table
    missing = [t for t in TABLE_ORDER if t in src_tables and t not in dst_tables]
    if missing:
        print(f"ERROR: tables in SQLite but not in Postgres: {missing}")
        print("Run `alembic upgrade head` first to create the schema.")
        sys.exit(1)

    with dst.begin() as conn:
        for table_name in TABLE_ORDER:
            if table_name not in src_tables:
                continue  # table doesn't exist in source (e.g. empty overlay)
            if table_name not in dst_tables:
                continue  # table doesn't exist in destination (e.g. overlay not installed)

            rows_raw = conn.execute(select("*").select_from(text(table_name))).fetchall()
            if rows_raw:
                print(f"  WARNING: {table_name} already has {len(rows_raw)} rows — skipping")
                continue

            src_rows = [
                dict(r._mapping)
                for r in src.connect().execute(select("*").select_from(text(table_name)))
            ]
            if not src_rows:
                print(f"  {table_name}: 0 rows (empty)")
                continue

            conn.execute(insert(text(table_name)), src_rows)
            print(f"  {table_name}: {len(src_rows)} rows migrated")

    # Verification
    print()
    failures = 0
    for table_name in TABLE_ORDER:
        if table_name not in src_tables:
            continue
        if table_name not in dst_tables:
            continue
        src_count = _row_count(src, table_name)
        dst_count = _row_count(dst, table_name)
        status = "OK" if src_count == dst_count else "MISMATCH"
        if status != "OK":
            failures += 1
        print(f"  {table_name}: SQLite={src_count} Postgres={dst_count} [{status}]")

    print()
    print(f"Migration {'PASSED' if failures == 0 else 'FAILED'} (errors={failures})")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    sqlite_url = os.environ.get("SQLITE_URL")
    pg_url = os.environ.get("PG_URL")
    if not sqlite_url or not pg_url:
        print("Usage: SQLITE_URL=... PG_URL=... python -m api.db.migrate_data", file=sys.stderr)
        sys.exit(2)
    migrate(sqlite_url=sqlite_url, pg_url=pg_url)
