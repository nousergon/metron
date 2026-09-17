"""The glance aggregate endpoint's p95 latency (metron-ops-I327, Stage A exit gate O2):
parsing the router's timing log lines, the p95 math (including the empty -> not-measured
case), the durable upsert, and the ``/meta/status`` surface that reads it back without an
ad-hoc query.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

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

    latest = gl.latest(db_session)
    assert latest["status"] == "measured"
    assert latest["trading_day"] == "2026-09-16"
    assert latest["n"] == 3  # the second call's result, not the first — an upsert not a second row


def test_record_refuses_a_not_measured_result():
    empty = gl.compute([], trading_day=date(2026, 9, 16))
    import pytest

    with pytest.raises(ValueError, match="not-measured"):
        gl.record(None, empty)  # never reaches the session — raises before touching it


def test_latest_is_not_measured_with_no_recorded_day(db_session):
    assert gl.latest(db_session) == {
        "status": "not-measured", "trading_day": None, "p95_ms": None, "n": None, "target_ms": 1000.0,
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

        gl.record(db_session, gl.compute([SUCCESS_LINE], trading_day=date(2026, 9, 16)))
        r = client.get("/meta/status")
        perf = r.json()["performance"]["glance_p95"]
        assert perf["status"] == "measured"
        assert perf["p95_ms"] == 812.3
        assert perf["within_target"] is True  # 812.3ms is within the O2 <= 1.0s gate
    finally:
        app.dependency_overrides.pop(get_session, None)


# ── CLI script (scripts/glance_p95.py) ───────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(*args: str, input_text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "glance_p95.py"), *args],
        input=input_text, capture_output=True, text=True, cwd=_REPO_ROOT,
    )


def test_cli_computes_p95_from_stdin_without_recording():
    proc = _run_cli("--date", "2026-09-16", input_text=f"{SUCCESS_LINE}\n{FAILURE_LINE}\n")
    assert proc.returncode == 0, proc.stderr
    assert '"status": "measured"' in proc.stdout
    assert '"trading_day": "2026-09-16"' in proc.stdout


def test_cli_empty_input_is_not_measured_and_record_exits_nonzero():
    proc = _run_cli("--date", "2026-09-16", "--record", input_text=UNRELATED_LINE)
    assert proc.returncode == 1
    assert '"status": "not-measured"' in proc.stdout
