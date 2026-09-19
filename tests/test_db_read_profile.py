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
