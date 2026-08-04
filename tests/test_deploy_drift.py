"""The box-behind-origin/main detector (metron-ops#268).

`deploy.yml` failed on every run for four days and nothing said so, because a deploy that
dies before `systemctl restart` leaves the previously deployed services up and
health-checking green. These tests pin the predicate that would have caught it, and the
non-zero CLI exit that makes the systemd unit red.
"""

from __future__ import annotations

import pytest

from api.services import alerting, deploy_drift
from api.services.deploy_drift import RepoState, is_drifted


def _state(head="aaa", remote="bbb", behind=1, age=120) -> RepoState:
    return RepoState(path="/repo", head=head, remote=remote, remote_age_min=age, behind=behind)


def test_box_at_origin_main_is_not_drifted():
    assert not is_drifted(_state(head="aaa", remote="aaa", behind=0, age=999))


def test_one_commit_past_the_grace_window_is_drift():
    """One undeployed commit is the condition. A threshold above 1 would have stayed
    silent through the outage this exists for — only four commits landed in four days."""
    assert is_drifted(_state(behind=1, age=31), grace_minutes=30)


def test_a_deploy_in_flight_is_not_drift():
    """A green deploy takes ~100s end to end; paging on the gap between merge and restart
    would make this check noise and get it ignored."""
    assert not is_drifted(_state(behind=1, age=5), grace_minutes=30)


def test_a_box_ahead_of_origin_is_not_reported_as_a_broken_deploy():
    """A hand-edited or mid-rebase checkout differs from origin/main without any deploy
    having failed. Reporting it as 'deploys are broken' would be a false statement."""
    assert not is_drifted(_state(head="aaa", remote="bbb", behind=0, age=999))


def test_check_reports_only_the_drifted_repos(monkeypatch):
    states = {
        "/repo/fresh": _state(head="s", remote="s", behind=0, age=0),
        "/repo/stale": _state(head="old", remote="new", behind=3, age=600),
    }
    monkeypatch.setattr(deploy_drift, "read_state", lambda p: states[p])
    out = deploy_drift.check(("/repo/fresh", "/repo/stale"))
    assert [s.behind for s in out] == [3]


def test_a_git_failure_is_not_reported_as_healthy(monkeypatch):
    """The dangerous failure mode for any detector: erroring into 'all clear'. read_state
    raises, and check() must let it propagate so the unit goes red."""
    def boom(_path):
        raise RuntimeError("git fetch failed")

    monkeypatch.setattr(deploy_drift, "read_state", boom)
    with pytest.raises(RuntimeError):
        deploy_drift.check(("/repo/any",))


def test_cli_exits_non_zero_on_drift(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        deploy_drift, "check",
        lambda **kw: [_state(head="old", remote="new", behind=4, age=5760)],
    )
    assert deploy_drift.main([]) == 1
    text, kwargs = sent[0]
    assert "behind" in text and "old" in text and "new" in text
    assert kwargs["severity"] == "error"


def test_cli_exits_zero_and_stays_silent_when_current(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append(t) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: [])
    assert deploy_drift.main([]) == 0
    assert sent == []


def test_cli_grace_minutes_reaches_the_check(monkeypatch):
    """A flag that silently fails to reach the predicate is the same class of defect as a
    detector wired to a channel that does not exist."""
    seen: dict = {}
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: seen.update(kw) or [])
    deploy_drift.main(["--grace-minutes", "5"])
    assert seen["grace_minutes"] == 5


def test_the_drift_check_does_not_import_the_database_layer():
    """The regression this module was moved out of api.maintenance to prevent.

    The systemd unit loads only the repo-root env file, which still names SQLite, so the
    import-time guard in api.db.session (metron-ops#264) killed
    `python -m api.maintenance deploy-drift-check` before it checked anything — the unit
    went red for a reason that had nothing to do with drift. A subprocess is used because
    the pytest process has already imported half the app.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import api.services.deploy_drift as d; "
        "bad=[m for m in sys.modules if m.startswith('api.db')]; "
        "print('DB_MODULES=' + ','.join(sorted(bad)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    assert "DB_MODULES=" in out
    assert out.strip() == "DB_MODULES=", (
        f"api.services.deploy_drift pulled in the database layer: {out.strip()}. The "
        "drift check must keep working when the database is the thing that is broken."
    )
