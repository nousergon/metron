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


# ── The monitor must not write the refs the deploy writes (metron-ops#268 follow-up) ──
#
# 2026-08-27 20:07 UTC: this check's `git fetch origin` and deploy.yml's `git fetch origin`
# ran in the same second in the same working copy. The deploy lost the ref lock:
#   error: cannot lock ref 'refs/remotes/origin/main': is at 95cd989 but expected 0f2a6b8
# and died before deploy-on-merge.sh started, so even the deploy script's own failure trap
# never fired. 95cd989 sat undeployed for five hours while this check reported the drift it
# had caused. These tests pin the two properties that make that impossible; each fails if
# read_state goes back to a bare `git fetch origin`.


def _record_git(monkeypatch, responses):
    """Capture every git argv read_state issues, answering from `responses` by subcommand."""
    calls: list[tuple[str, ...]] = []

    def fake(path, *args):
        calls.append(args)
        for prefix, out in responses.items():
            if args[: len(prefix)] == prefix:
                return out
        return ""

    monkeypatch.setattr(deploy_drift, "_git", fake)
    return calls


def test_the_healthy_path_writes_nothing_at_all(monkeypatch):
    """Box is current — the hourly case, ~24 runs a day against a live deploy target.

    It must not fetch, because a fetch is a ref write and a ref write is a lock the deploy
    can lose. `ls-remote` answers the only question this path asks.
    """
    sha = "a" * 40
    calls = _record_git(monkeypatch, {
        ("ls-remote",): f"{sha}\trefs/heads/main",
        ("rev-parse", "HEAD"): sha,
        ("rev-parse", "--short", "HEAD"): sha[:7],
    })
    state = deploy_drift.read_state("/repo")
    assert state.behind == 0 and state.head == state.remote
    assert not [c for c in calls if c[0] == "fetch"], f"healthy path fetched: {calls}"


def test_the_drifted_path_fetches_only_into_the_private_ref(monkeypatch):
    """Drift needs history, so this path does fetch — into a ref nothing else touches,
    and without FETCH_HEAD, which is the other file two concurrent fetches fight over."""
    head, remote = "a" * 40, "b" * 40
    calls = _record_git(monkeypatch, {
        ("ls-remote",): f"{remote}\trefs/heads/main",
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--short", "HEAD"): head[:7],
        ("rev-parse", "--short", deploy_drift.DRIFT_REF): remote[:7],
        ("rev-list",): "1",
        ("log",): "1",
    })
    deploy_drift.read_state("/repo")
    fetches = [c for c in calls if c[0] == "fetch"]
    assert len(fetches) == 1, f"expected exactly one fetch, got {fetches}"
    args = fetches[0]
    assert "--no-write-fetch-head" in args
    assert "--refmap=" in args, (
        "an explicit refspec is NOT enough — git opportunistically applies the remote's "
        "configured refmap on top of it and updates refs/remotes/origin/main anyway"
    )
    assert f"refs/heads/main:{deploy_drift.DRIFT_REF}" in args
    assert not any(a == "origin" and i == len(args) - 1 for i, a in enumerate(args)), (
        "a trailing bare `origin` means the default refspec — that writes "
        "refs/remotes/origin/* and is exactly the collision this fixes"
    )


def test_against_real_git_origin_main_is_left_where_the_deploy_put_it(tmp_path):
    """The guard that exercises git itself rather than a mock of it.

    Stages the live shape: a deployed working copy whose `refs/remotes/origin/main` is a
    fact the deploy owns, and a newer commit on the remote. After the check runs,
    origin/main must be byte-identical to what it was — the check may learn the remote
    moved, it may not be the thing that records it.
    """
    import subprocess

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True, timeout=120
        ).stdout.strip()

    remote, box, author = tmp_path / "remote.git", tmp_path / "box", tmp_path / "author"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "--quiet", str(remote), str(author)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(author, "config", k, v)
    (author / "f").write_text("A")
    git(author, "add", "f")
    git(author, "commit", "--quiet", "-m", "A")
    git(author, "push", "--quiet", "origin", "main")

    subprocess.run(["git", "clone", "--quiet", str(remote), str(box)], check=True)
    before = git(box, "rev-parse", "refs/remotes/origin/main")

    (author / "f").write_text("B")
    git(author, "commit", "--quiet", "-am", "B")
    git(author, "push", "--quiet", "origin", "main")

    state = deploy_drift.read_state(str(box))

    assert state.behind == 1, "the check must still see the undeployed commit"
    assert git(box, "rev-parse", "refs/remotes/origin/main") == before, (
        "the drift check moved refs/remotes/origin/main — that write is the ref lock the "
        "deploy loses, and losing it strands the commit the deploy was landing"
    )
    assert git(box, "rev-parse", deploy_drift.DRIFT_REF) == git(author, "rev-parse", "HEAD")
