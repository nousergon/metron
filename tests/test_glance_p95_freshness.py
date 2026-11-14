"""Dead-man monitor for the glance p95 measurement job (metron-ops-I341): the SEPARATE,
independently-scheduled check that pages when the exit-gate figure has gone stale or the
measurement job has stopped firing -- distinct from the job itself, which cannot report
its own absence.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from api.services import glance_latency as gl
from api.services import glance_p95_freshness as freshness

SUCCESS_LINE = (
    "glance composed portfolio=11111111-1111-1111-1111-111111111111 duration_ms=812.3 "
    "state=open tier=personal feed=True degraded=0 producers=9 zones_ms={}"
)

_NY = ZoneInfo("America/New_York")


def test_healthy_when_freshly_measured_and_recently_run(db_session):
    day = date(2026, 9, 16)
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=day))
    gl.record_run(db_session, target_day=day, status="measured", n=1, p95_ms=812.3)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=_NY)
    assert freshness.check(db_session, now=now) is None


def test_unhealthy_when_no_run_has_ever_happened(db_session):
    finding = freshness.check(db_session, now=datetime(2026, 9, 17, 12, 0, tzinfo=_NY))
    assert finding is not None
    assert any("never run" in r for r in finding["reasons"])


def test_unhealthy_when_the_last_run_is_older_than_the_grace_window(db_session):
    day = date(2026, 9, 10)
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=day))
    row = gl.record_run(db_session, target_day=day, status="measured", n=1, p95_ms=812.3)
    # record_run's `ran_at` is DB-generated (server_default=func.now()), i.e. the real
    # wall clock at insert time — correct in production, but this test needs to simulate
    # an OLD run, so it backdates the row directly rather than through the API.
    row.ran_at = datetime(2026, 9, 10, 21, 0)
    db_session.commit()
    now = datetime(2026, 9, 17, 12, 0, tzinfo=_NY)  # 7 days after the last run
    finding = freshness.check(db_session, now=now)
    assert finding is not None
    assert any("appears to have stopped firing" in r for r in finding["reasons"])


def test_unhealthy_when_measured_but_stale_even_with_a_recent_run(db_session):
    # A run happened recently (status not-measured, e.g. a holiday) but the last
    # genuinely MEASURED day has fallen behind the cadence.
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=date(2026, 9, 1)))
    now = datetime(2026, 9, 17, 12, 0, tzinfo=_NY)
    gl.record_run(db_session, target_day=date(2026, 9, 16), status="not-measured", n=0)
    finding = freshness.check(db_session, now=now)
    assert finding is not None
    assert any("stale" in r for r in finding["reasons"])


def test_report_sends_an_alert_when_unhealthy_and_nothing_when_healthy(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "api.services.alerting.send_alert",
        lambda *a, **k: sent.append((a, k)) or True,
    )

    now = datetime(2026, 9, 17, 12, 0, tzinfo=_NY)
    finding = freshness.report(db_session, now=now)
    assert finding is not None
    assert len(sent) == 1
    assert sent[0][1]["severity"] == "error"
    assert sent[0][1]["dedup_key"] == "metron-glance-p95-freshness"

    sent.clear()
    day = date(2026, 9, 16)
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=day))
    gl.record_run(db_session, target_day=day, status="measured", n=1, p95_ms=812.3)
    finding = freshness.report(db_session, now=now)
    assert finding is None
    assert sent == []


def test_report_dry_run_detects_for_real_but_suppresses_the_send(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "api.services.alerting.send_alert",
        lambda *a, **k: calls.append(k.get("dry_run")) or True,
    )
    now = datetime(2026, 9, 17, 12, 0, tzinfo=_NY)
    finding = freshness.report(db_session, now=now, dry_run=True)
    assert finding is not None  # detection still ran for real
    assert calls == [True]  # but the send call was told to suppress


def test_unhealthy_when_trading_days_run_but_nothing_was_ever_measured(db_session):
    # metron-ops-I344: a not-measured run now exits 0, so this monitor is where a run of
    # silent trading days is graded. Nothing measured + two quiet trading days = finding.
    gl.record_run(db_session, target_day=date(2026, 11, 2), status="not-measured", n=0)  # Mon
    gl.record_run(db_session, target_day=date(2026, 11, 3), status="not-measured", n=0)  # Tue
    finding = freshness.check(db_session, now=datetime(2026, 11, 4, 12, 0, tzinfo=_NY))
    assert finding is not None
    assert finding["kind"] == "no_traffic"
    assert any("no glance requests on 2 trading day(s)" in r for r in finding["reasons"])


def test_days_before_the_parser_fix_are_not_evidence(db_session):
    # Until metron#593 (2026-10-30) the parser could not match production lines, so
    # those not-measured days say nothing about traffic.
    gl.record_run(db_session, target_day=date(2026, 10, 28), status="not-measured", n=0)
    gl.record_run(db_session, target_day=date(2026, 10, 29), status="not-measured", n=0)
    gl.record_run(db_session, target_day=date(2026, 10, 30), status="not-measured", n=0)
    assert freshness.check(db_session, now=datetime(2026, 10, 31, 12, 0, tzinfo=_NY)) is None


def test_alert_says_issue_impact_fix_and_action(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "api.services.alerting.send_alert",
        lambda *a, **k: sent.append(a[0]) or True,
    )
    gl.record_run(db_session, target_day=date(2026, 11, 2), status="not-measured", n=0)
    gl.record_run(db_session, target_day=date(2026, 11, 3), status="not-measured", n=0)
    freshness.report(db_session, now=datetime(2026, 11, 4, 12, 0, tzinfo=_NY))
    lines = sent[0].splitlines()
    assert lines[0].startswith("Metron glance p95 has no reading: no glance requests")
    assert [ln.split(":")[0] for ln in lines[1:]] == [
        "Impact", "Auto-fix", "Resolution", "You",
    ]


def test_broken_job_alert_names_the_sweep(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "api.services.alerting.send_alert",
        lambda *a, **k: sent.append(a[0]) or True,
    )
    freshness.report(db_session, now=datetime(2026, 9, 17, 12, 0, tzinfo=_NY))
    assert sent[0].startswith("Metron glance p95 job is broken:")
    assert "Auto-fix: tonight's sweep" in sent[0]
    assert sent[0].endswith("You: nothing.")


def test_quiet_weekend_never_pages_when_nothing_was_ever_measured(db_session):
    # Saturday and Sunday are not trading days; one quiet trading day is tolerated.
    gl.record_run(db_session, target_day=date(2026, 9, 18), status="not-measured", n=0)  # Fri
    gl.record_run(db_session, target_day=date(2026, 9, 19), status="not-measured", n=0)  # Sat
    gl.record_run(db_session, target_day=date(2026, 9, 20), status="not-measured", n=0)  # Sun
    assert freshness.check(db_session, now=datetime(2026, 9, 21, 12, 0, tzinfo=_NY)) is None


def test_quiet_trading_days_do_not_add_a_finding_once_a_day_was_measured(db_session):
    # Once a day has been measured, the stale check owns cadence; this condition is only
    # for the never-measured case, so it must not double-report.
    day = date(2026, 9, 22)
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=day))
    gl.record_run(db_session, target_day=date(2026, 9, 18), status="not-measured", n=0)
    gl.record_run(db_session, target_day=date(2026, 9, 21), status="not-measured", n=0)
    gl.record_run(db_session, target_day=day, status="measured", n=1, p95_ms=812.3)
    assert freshness.check(db_session, now=datetime(2026, 9, 23, 12, 0, tzinfo=_NY)) is None


def test_error_runs_are_job_broken_not_no_traffic(db_session):
    # metron-ops-I392: a run that errored took no reading, so two quiet days with an error
    # among the runs is the job failing, not an absence of glance traffic.
    gl.record_run(db_session, target_day=date(2026, 11, 2), status="not-measured", n=0)  # Mon
    gl.record_run(db_session, target_day=date(2026, 11, 3), status="not-measured", n=0)  # Tue
    gl.record_run(db_session, target_day=date(2026, 11, 4), status="error", error="boom")  # Wed
    finding = freshness.check(db_session, now=datetime(2026, 11, 5, 12, 0, tzinfo=_NY))
    assert finding is not None
    assert finding["kind"] == "job_broken"
    assert any("errored on 1 trading day(s)" in r for r in finding["reasons"])


def test_an_error_superseded_by_a_later_completed_run_is_not_job_broken(db_session):
    # metron-ops-I392 (066e5661): the run log is append-only, so an old error must not keep
    # a recovered job job_broken; quiet days after it are no traffic.
    gl.record_run(db_session, target_day=date(2026, 11, 2), status="error", error="boom")  # Mon
    gl.record_run(db_session, target_day=date(2026, 11, 3), status="not-measured", n=0)  # Tue
    gl.record_run(db_session, target_day=date(2026, 11, 4), status="not-measured", n=0)  # Wed
    finding = freshness.check(db_session, now=datetime(2026, 11, 5, 12, 0, tzinfo=_NY))
    assert finding is not None
    assert finding["kind"] == "no_traffic"
    assert not any("errored" in r for r in finding["reasons"])


def test_a_retry_of_the_errored_day_supersedes_the_error(db_session):
    gl.record_run(db_session, target_day=date(2026, 11, 3), status="not-measured", n=0)  # Tue
    gl.record_run(db_session, target_day=date(2026, 11, 4), status="error", error="boom")  # Wed
    gl.record_run(db_session, target_day=date(2026, 11, 4), status="not-measured", n=0)  # retry
    finding = freshness.check(db_session, now=datetime(2026, 11, 5, 12, 0, tzinfo=_NY))
    assert finding is not None
    assert finding["kind"] == "no_traffic"
