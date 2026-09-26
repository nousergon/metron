"""metron-ops-I343: the run profile measures, and never costs the run it measures.

The defect this instruments: `metron-prod` egresses a metronomic 0.225-0.236 GB
every day and has since 2026-08-02, blew its 5 GB ceiling on 2026-08-22 and
finished August at 7.13 GB. The database is 66 MB and is awake ~72 min/day, so
each scheduled run reads most of it. These tests hold the properties that make
the resulting artifact trustworthy.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.services import db_read_profile as drp


def test_a_block_records_calls_rows_and_time():
    p = drp.ReadProfile("daily-refresh")
    with p.block("close_history") as record:
        record(1200)
    with p.block("close_history") as record:
        record(800)
    b = p.blocks["close_history"]
    assert b.calls == 2
    assert b.rows == 2000
    assert b.seconds >= 0.0


def test_a_raising_block_still_contributes_its_cost():
    """A block that blew up after reading 40k rows read them regardless. Dropping
    that is exactly how the expensive call hides in the profile."""
    p = drp.ReadProfile("daily-refresh")
    with pytest.raises(RuntimeError):
        with p.block("expensive") as record:
            record(40_000)
            raise RuntimeError("boom")
    assert p.blocks["expensive"].calls == 1
    assert p.blocks["expensive"].rows == 40_000


def test_a_block_that_counts_nothing_reports_zero_not_absent():
    """Timed but uncounted is an honest state, and a different one from absent.
    It is why rows and seconds are reported side by side rather than derived."""
    p = drp.ReadProfile("daily-refresh")
    with p.block("timed_only"):
        pass
    assert p.blocks["timed_only"].rows == 0
    assert p.blocks["timed_only"].calls == 1


def test_blocks_are_ordered_loudest_first():
    p = drp.ReadProfile("daily-refresh")
    with p.block("cheap") as record:
        record(1)
    b = p.blocks.setdefault("pricey", drp._Block("pricey"))
    b.calls, b.rows, b.seconds = 1, 99, 10.0
    labels = [x["label"] for x in p.as_dict()["blocks"]]
    assert labels[0] == "pricey"


def test_the_payload_carries_a_schema_version():
    """A reader keys off this rather than guessing from shape."""
    assert drp.ReadProfile("daily-refresh").as_dict()["schema_version"] == drp.SCHEMA_VERSION


def test_the_payload_reports_no_bytes():
    """Bytes are NOT measured: nothing in the SQLAlchemy/psycopg path exposes wire
    bytes per statement, and a row-width estimate would read as a measurement while
    being a guess. The Neon monitor's data_transfer_bytes stays the authority on the
    total. If a future change adds a bytes field it must be a real measurement, and
    this test is the place that argument gets had."""
    payload = drp.ReadProfile("daily-refresh").as_dict()
    assert not any("byte" in k for k in payload)
    assert all(not any("byte" in k for k in b) for b in payload["blocks"])


class _FakeS3:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.puts: list[tuple[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803 — boto3 kwarg names
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append((Bucket, Key))


def test_publish_writes_a_dated_object_and_latest():
    p = drp.ReadProfile("daily-refresh")
    with p.block("x") as record:
        record(5)
    s3 = _FakeS3()
    key = drp.publish(p, bucket="alpha-engine-research", s3_client=s3,
                      now=datetime(2026, 9, 19, 20, 45, 0, tzinfo=UTC))
    assert key == "metron/db_read_profile/daily-refresh/2026-09-19T20-45-00Z.json"
    assert [k for _b, k in s3.puts] == [
        "metron/db_read_profile/daily-refresh/2026-09-19T20-45-00Z.json",
        "metron/db_read_profile/daily-refresh/latest.json",
    ]


def test_publish_writes_under_the_granted_prefix():
    """`alpha-engine-dashboard-role`'s `alpha-engine-research-access` policy grants
    s3:PutObject on `arn:aws:s3:::alpha-engine-research/metron/*` — verified against
    the live role 2026-09-19. A key outside that prefix is AccessDenied forever, and
    this change ships no IAM."""
    assert drp.KEY_PREFIX.startswith("metron/")


def test_a_failed_publish_never_raises():
    """The run's deliverable is refreshed prices and NAV snapshots. Losing a profile
    must not cost them — the swallow is deliberate and the WARN is its recording
    surface."""
    p = drp.ReadProfile("daily-refresh")
    assert drp.publish(p, bucket="alpha-engine-research", s3_client=_FakeS3(fail=True)) is None


def test_a_failed_publish_says_what_failed(caplog):
    import logging

    caplog.set_level(logging.WARNING)
    drp.publish(drp.ReadProfile("daily-refresh"), bucket="b", s3_client=_FakeS3(fail=True))
    assert "AccessDenied" in caplog.text
    assert "db-read-profile" in caplog.text


# ── driver-level SELECT counting (schema v2) ─────────────────────────────────
# The 2026-09-24 refresh profiles showed best_effort:risk / performance /
# attribution at 110-180 s each with rows=0: they return domain objects, so the
# call-site counter had nothing to report and the slowest regions were invisible.


def test_a_select_is_charged_to_the_innermost_open_block():
    p = drp.ReadProfile("daily-refresh")
    with p.block("best_effort:risk"):
        p.count_statement("SELECT * FROM close_history WHERE symbol = ?", 1200)
        with p.block("inner"):
            p.count_statement("  select 1", 3)
    assert (p.blocks["best_effort:risk"].statements, p.blocks["best_effort:risk"].db_rows) == (1, 1200)
    assert (p.blocks["inner"].statements, p.blocks["inner"].db_rows) == (1, 3)


def test_reads_outside_any_block_still_count_toward_the_total():
    p = drp.ReadProfile("daily-refresh")
    p.count_statement("WITH x AS (SELECT 1) SELECT * FROM x", 7)
    payload = p.as_dict()
    assert payload["total_db_rows"] == 7
    assert payload["total_statements"] == 1
    assert drp.UNBLOCKED_LABEL in p.blocks


def test_writes_are_not_reads():
    p = drp.ReadProfile("daily-refresh")
    with p.block("snapshots"):
        for stmt in ("INSERT INTO nav_snapshots VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t", ""):
            p.count_statement(stmt, 50)
    assert p.blocks["snapshots"].statements == 0
    assert p.blocks["snapshots"].db_rows == 0


def test_an_unknown_rowcount_is_a_statement_not_a_guess():
    """SQLite reports rowcount -1 for a SELECT. That is recorded as a statement
    with no row count, never as a negative or invented row count."""
    p = drp.ReadProfile("daily-refresh")
    with p.block("x"):
        p.count_statement("SELECT 1", -1)
    assert (p.blocks["x"].statements, p.blocks["x"].db_rows) == (1, 0)


def test_watch_counts_real_statements_and_detaches_on_exit(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    p = drp.ReadProfile("daily-refresh")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER)"))
        with p.watch(engine), p.block("reads"):
            conn.execute(text("SELECT a FROM t")).all()
            conn.execute(text("SELECT a FROM t")).all()
        conn.execute(text("SELECT a FROM t")).all()  # after exit: not counted
    assert p.blocks["reads"].statements == 2
    assert p.as_dict()["total_statements"] == 2  # the third SELECT, after exit, is not counted


def test_watch_detaches_even_when_the_run_raises(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    p = drp.ReadProfile("daily-refresh")
    with pytest.raises(RuntimeError):
        with p.watch(engine):
            raise RuntimeError("boom")
    with engine.connect() as conn:
        conn.execute(text("SELECT 1")).all()
    assert p.as_dict()["total_statements"] == 0
