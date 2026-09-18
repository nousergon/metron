"""The glance aggregate endpoint's p95 latency (metron-ops-I327, Stage A exit gate O2):
parsing the router's timing log lines, the p95 math (including the empty -> not-measured
case), the durable upsert, and the ``/meta/status`` surface that reads it back without an
ad-hoc query.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from krepis.trading_calendar import last_closed_trading_day
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.services import glance_latency as gl

SUCCESS_LINE = (
    f"glance composed portfolio={uuid.uuid4()} duration_ms=812.3 state=open tier=personal feed=True "
    "degraded=0 producers=9 zones_ms={}"
)
FAILURE_LINE = f"glance failed portfolio={uuid.uuid4()} duration_ms=45.0 tier=beta feed=False"
UNRELATED_LINE = "unrelated log line duration_ms=999.0"


# ── parsing + p95 math ───────────────────────────────────────────────────────


def test_parse_line_reads_duration_from_both_success_and_failure_records():
    assert gl.parse_line(SUCCESS_LINE) == 812.3
    assert gl.parse_line(FAILURE_LINE) == 45.0


def test_parse_line_ignores_an_unrelated_log_line():
    assert gl.parse_line(UNRELATED_LINE) is None


def test_p95_is_nearest_rank_over_a_fixture_set():
    # 20 values 1..20 -> ceil(20*0.95) = 19th smallest = 19.
    assert gl.p95([float(i) for i in range(1, 21)]) == 19.0


def test_p95_single_value_is_itself():
    assert gl.p95([500.0]) == 500.0


def test_p95_empty_is_not_measured():
    assert gl.p95([]) is None


def test_compute_over_a_day_of_lines_including_the_empty_case():
    lines = [SUCCESS_LINE, FAILURE_LINE, UNRELATED_LINE, SUCCESS_LINE]
    result = gl.compute(lines, trading_day=date(2026, 9, 16))
    assert result.n == 3
    assert result.status == "measured"
    assert result.p95_ms is not None

    empty = gl.compute([UNRELATED_LINE], trading_day=date(2026, 9, 16))
    assert empty.n == 0
    assert empty.p95_ms is None
    assert empty.status == "not-measured"


# ── persistence + surface ────────────────────────────────────────────────────


def test_record_upserts_one_row_per_trading_day(db_session):
    day = date(2026, 9, 16)
    r1 = gl.compute([SUCCESS_LINE], trading_day=day)
    gl.record(db_session, r1)
    r2 = gl.compute([SUCCESS_LINE, SUCCESS_LINE, FAILURE_LINE], trading_day=day)
    gl.record(db_session, r2)

    # `now` pinned just after `day` so the fixture's fixed historical date reads as
    # current regardless of when this suite actually runs (metron-ops-I341 freshness).
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    latest = gl.latest(db_session, now=now)
    assert latest["status"] == "measured"
    assert latest["trading_day"] == "2026-09-16"
    assert latest["n"] == 3  # the second call's result, not the first — an upsert not a second row


def test_record_refuses_a_not_measured_result():
    empty = gl.compute([], trading_day=date(2026, 9, 16))

    with pytest.raises(ValueError, match="not-measured"):
        gl.record(None, empty)  # never reaches the session — raises before touching it


def test_latest_is_not_measured_with_no_recorded_day(db_session):
    assert gl.latest(db_session) == {
        "status": "not-measured", "trading_day": None, "p95_ms": None, "n": None,
        "target_ms": 1000.0, "stale": False, "last_run": None,
    }


def test_meta_status_surfaces_the_recorded_p95(client, db_session, monkeypatch):
    from api.db.session import get_session
    from api.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    try:
        r = client.get("/meta/status")
        assert r.json()["performance"]["glance_p95"]["status"] == "not-measured"

        # last_closed_trading_day() so this stays fresh (not "stale") no matter when
        # the suite runs — /meta/status calls latest() with no `now` override, i.e.
        # real wall-clock time.
        today = last_closed_trading_day()
        gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=today))
        r = client.get("/meta/status")
        perf = r.json()["performance"]["glance_p95"]
        assert perf["status"] == "measured"
        assert perf["stale"] is False
        assert perf["p95_ms"] == 812.3
        assert perf["within_target"] is True  # 812.3ms is within the O2 <= 1.0s gate
    finally:
        app.dependency_overrides.pop(get_session, None)


# ── freshness + the execution (run log) record (metron-ops-I341) ────────────


def test_stale_when_the_measured_day_has_fallen_behind_the_cadence(db_session):
    gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=date(2026, 9, 1)))
    # `now` far past 2026-09-01 — more than one trading day's grace — so the row reads
    # stale rather than current. The last known figure is still returned.
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    latest = gl.latest(db_session, now=now)
    assert latest["status"] == "stale"
    assert latest["stale"] is True
    assert latest["p95_ms"] == 812.3  # last known figure — never dropped, never zeroed


def test_record_run_is_append_only_and_distinguishes_a_quiet_day_from_a_missed_run(db_session):
    day = date(2026, 9, 16)
    gl.record_run(db_session, target_day=day, status="not-measured", n=0)
    run = gl.last_run(db_session)
    assert run["status"] == "not-measured"
    assert run["target_day"] == "2026-09-16"

    # A second invocation appends rather than upserts — each run is its own fact.
    gl.record_run(db_session, target_day=day, status="error", error="boom")
    run2 = gl.last_run(db_session)
    assert run2["status"] == "error"
    assert run2["error"] == "boom"


def test_record_run_rejects_an_unknown_status(db_session):
    with pytest.raises(ValueError, match="measured/not-measured/error"):
        gl.record_run(db_session, target_day=date(2026, 9, 16), status="bogus")


def test_latest_surfaces_last_run_even_when_the_day_was_never_measured(db_session):
    gl.record_run(db_session, target_day=date(2026, 9, 16), status="not-measured", n=0)
    latest = gl.latest(db_session)
    assert latest["status"] == "not-measured"  # no GlanceLatencyDaily row was ever written
    assert latest["last_run"]["status"] == "not-measured"  # but the job DID run — distinguishable


# ── CLI script (scripts/glance_p95.py) ───────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str, input_text: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    # A bare `python scripts/glance_p95.py` puts the script's own directory
    # (scripts/), not this worktree's repo root, at sys.path[0] — so `import api...`
    # would silently resolve to whatever `api` package the venv has editable-installed
    # (the ORIGINAL checkout, not this worktree, under concurrent-session worktree
    # isolation) rather than the code this test is actually exercising. PYTHONPATH
    # pins it to this worktree explicitly.
    full_env = {**(env if env is not None else os.environ), "PYTHONPATH": str(_REPO_ROOT)}
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "glance_p95.py"), *args],
        input=input_text, capture_output=True, text=True, cwd=_REPO_ROOT, env=full_env,
    )


def test_cli_computes_p95_from_stdin_without_recording():
    # No --record: the dry-run path never opens a DB session, so this needs no DB env.
    proc = _run_cli("--date", "2026-09-16", input_text=f"{SUCCESS_LINE}\n{FAILURE_LINE}\n")
    assert proc.returncode == 0, proc.stderr
    assert '"status": "measured"' in proc.stdout
    assert '"trading_day": "2026-09-16"' in proc.stdout


@pytest.fixture()
def cli_db_env(tmp_path):
    """A real, migrated SQLite DB + the env pointing the CLI subprocess at it
    (metron-ops-I341) — ``--record`` now always opens a session (it appends a
    ``GlanceP95RunLog`` row on every outcome, not only a measured one), so the CLI
    subprocess tests need a genuinely working database rather than the in-process
    ``db_session`` fixture's isolated in-memory engine, which a subprocess can't see.
    ``ENV=test`` satisfies ``api.db.session``'s non-dev-SQLite guard (metron-ops#264)."""
    db_path = tmp_path / "cli_glance_p95.sqlite"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "ENV": "test"}
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True,
    )
    assert migrate.returncode == 0, migrate.stderr
    return env


def test_cli_empty_input_is_not_measured_and_record_exits_nonzero(cli_db_env):
    proc = _run_cli("--date", "2026-09-16", "--record", input_text=UNRELATED_LINE, env=cli_db_env)
    assert proc.returncode == 1, proc.stderr
    assert '"status": "not-measured"' in proc.stdout


def test_cli_record_appends_a_run_log_row_on_a_measured_day(cli_db_env):
    proc = _run_cli(
        "--date", "2026-09-16", "--record", input_text=f"{SUCCESS_LINE}\n", env=cli_db_env,
    )
    assert proc.returncode == 0, proc.stderr

    engine = create_engine(cli_db_env["DATABASE_URL"])
    session = sessionmaker(bind=engine)()
    try:
        from api.services import glance_latency as gl2

        run = gl2.last_run(session)
        assert run is not None
        assert run["status"] == "measured"
        assert run["target_day"] == "2026-09-16"
    finally:
        session.close()


def test_cli_record_appends_a_not_measured_run_log_row(cli_db_env):
    proc = _run_cli("--date", "2026-09-16", "--record", input_text=UNRELATED_LINE, env=cli_db_env)
    assert proc.returncode == 1, proc.stderr

    engine = create_engine(cli_db_env["DATABASE_URL"])
    session = sessionmaker(bind=engine)()
    try:
        from api.services import glance_latency as gl2

        run = gl2.last_run(session)
        assert run is not None
        assert run["status"] == "not-measured"
    finally:
        session.close()
