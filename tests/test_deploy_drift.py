"""The box-behind-origin/main detector (metron-ops#268).

`deploy.yml` failed on every run for four days and nothing said so, because a deploy that
dies before `systemctl restart` leaves the previously deployed services up and
health-checking green. These tests pin the predicate that would have caught it, and the
non-zero CLI exit that makes the systemd unit red.
"""

from __future__ import annotations

import subprocess

import pytest

from api.services import alerting, deploy_drift
from api.services.deploy_drift import GitError, RepoState, Unmeasured, Verdict, is_drifted


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
    assert [s.behind for s in out.drifted] == [3]
    assert out.unmeasured == [] and out.exit_code == deploy_drift.EXIT_DRIFT


def test_an_unexpected_error_is_not_reported_as_healthy(monkeypatch):
    """The dangerous failure mode for any detector: erroring into 'all clear'. Anything
    that is not a named could-not-measure is a bug, and check() must let it propagate so
    the unit goes red with a traceback."""
    def boom(_path):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(deploy_drift, "read_state", boom)
    with pytest.raises(RuntimeError):
        deploy_drift.check(("/repo/any",))


def test_cli_exits_non_zero_on_drift(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        deploy_drift, "check",
        lambda **kw: Verdict(drifted=[_state(head="old", remote="new", behind=4, age=5760)]),
    )
    assert deploy_drift.main([]) == 1
    text, kwargs = sent[0]
    assert "behind" in text and "old" in text and "new" in text
    assert kwargs["severity"] == "error"
    assert kwargs["dry_run"] is False


def test_dry_run_still_detects_drift_and_exits_non_zero_but_sends_nothing(monkeypatch, capsys):
    """metron-ops-I340: --dry-run must still check for real and still exit non-zero on
    drift, and must pass dry_run=True all the way to send_alert so krepis short-circuits
    before it ever reaches SNS/Telegram."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(
        deploy_drift, "check",
        lambda **kw: Verdict(drifted=[_state(head="old", remote="new", behind=4, age=5760)]),
    )
    assert deploy_drift.main(["--dry-run"]) == 1
    text, kwargs = sent[0]
    assert "behind" in text and "old" in text and "new" in text
    assert kwargs["severity"] == "error"
    assert kwargs["dedup_key"] == "metron-deploy-drift"
    assert kwargs["dry_run"] is True
    assert "dry-run" in capsys.readouterr().out.lower()


def test_dry_run_exits_zero_and_sends_nothing_when_current(monkeypatch, capsys):
    sent: list[str] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append(t) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: Verdict())
    assert deploy_drift.main(["--dry-run"]) == 0
    assert sent == []
    assert "dry-run" in capsys.readouterr().out.lower()


def test_cli_exits_zero_and_stays_silent_when_current(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append(t) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: Verdict())
    assert deploy_drift.main([]) == 0
    assert sent == []


def test_cli_grace_minutes_reaches_the_check(monkeypatch):
    """A flag that silently fails to reach the predicate is the same class of defect as a
    detector wired to a channel that does not exist."""
    seen: dict = {}
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: seen.update(kw) or Verdict())
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


# ── ls-remote retries a transient network failure (2026-09-12 09:07 UTC exit-128) ──
#
# The hourly `ls-remote` died with git exit 128 once, and nothing else that hour was
# wrong — every other run that day reported healthy. The check paged CRITICAL and
# self-resolved an hour later. These tests pin: one failure then success stays healthy
# and touches no fetch; three failures still raise so a real outage reddens the unit;
# and the retries never reach `rev-parse`.


def _cpe(stderr="fatal: unable to access"):
    """What `_git` raises on a failed call — the shape remote_head retries on."""
    return GitError(f"git ls-remote origin refs/heads/main in /repo failed (exit 128): {stderr}")


def test_remote_head_retries_once_then_succeeds(monkeypatch):
    sha = "a" * 40
    calls: list[tuple] = []
    attempts = iter([_cpe(), f"{sha}\trefs/heads/main"])

    def fake_git(path, *args):
        calls.append(args)
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(deploy_drift, "_git", fake_git)
    sleeps: list[float] = []
    out = deploy_drift.remote_head("/repo", sleep=sleeps.append)

    assert out == sha
    assert len(calls) == 2
    assert sleeps == [deploy_drift.REMOTE_HEAD_BACKOFF_SEC[0]]


def test_remote_head_healthy_after_retry_writes_nothing_and_reports_healthy(monkeypatch):
    """A retried-but-recovered ls-remote must still be the write-free healthy path:
    exactly the two rev-parse calls it always makes, no fetch."""
    sha = "a" * 40
    attempts = iter([_cpe(), f"{sha}\trefs/heads/main"])

    def fake_git(path, *args):
        if args[:1] == ("ls-remote",):
            result = next(attempts)
            if isinstance(result, Exception):
                raise result
            return result
        if args == ("rev-parse", "HEAD"):
            return sha
        if args == ("rev-parse", "--short", "HEAD"):
            return sha[:7]
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(deploy_drift, "_git", fake_git)
    monkeypatch.setattr(deploy_drift.time, "sleep", lambda _s: None)
    state = deploy_drift.read_state("/repo")
    assert state.behind == 0 and state.head == state.remote


def test_remote_head_raises_after_exhausting_all_attempts(monkeypatch):
    """Three straight failures is a real outage, not noise — the unit must go red."""
    def fake_git(path, *args):
        raise _cpe()

    monkeypatch.setattr(deploy_drift, "_git", fake_git)
    sleeps: list[float] = []
    with pytest.raises(GitError):
        deploy_drift.remote_head("/repo", sleep=sleeps.append)
    assert sleeps == list(deploy_drift.REMOTE_HEAD_BACKOFF_SEC)


def test_remote_head_retries_never_reach_rev_parse(monkeypatch):
    """The retry loop is scoped to ls-remote alone — a failing remote must never cause an
    extra local `rev-parse` call, retried or otherwise."""
    calls: list[tuple] = []

    def fake_git(path, *args):
        calls.append(args)
        raise _cpe()

    monkeypatch.setattr(deploy_drift, "_git", fake_git)
    with pytest.raises(GitError):
        deploy_drift.remote_head("/repo", sleep=lambda _s: None)
    assert all(c[0] == "ls-remote" for c in calls)
    assert len(calls) == deploy_drift.REMOTE_HEAD_ATTEMPTS


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


# ── Could-not-measure is its own outcome (metron-ops#288, #291) ──
#
# 2026-09-06 and 2026-09-09: a failed `ls-remote` put only `CalledProcessError ... exit
# status 128` in the journal — git's stderr was captured and discarded — and box-health
# paged it exactly like drift. These tests pin: git's stderr reaches the error (redacted,
# one line), an unreadable repo is reported as could-not-measure with its own warning
# alert and exit 3, and neither in-sync (0) nor drift (1) changed.


def _raising_run(exc):
    def run(*_a, **_kw):
        raise exc
    return run


def test_git_failure_carries_git_stderr(monkeypatch):
    err = subprocess.CalledProcessError(
        128, ["git"], stderr="remote: Repository not found.\nfatal: repository not found\n",
    )
    monkeypatch.setattr(deploy_drift.subprocess, "run", _raising_run(err))
    with pytest.raises(GitError) as ei:
        deploy_drift._git("/repo", "ls-remote", "origin", "refs/heads/main")
    msg = str(ei.value)
    assert "git ls-remote origin refs/heads/main in /repo failed (exit 128)" in msg
    assert "remote: Repository not found. | fatal: repository not found" in msg
    assert "\n" not in msg, "the journal is read line by line"
    assert isinstance(ei.value, deploy_drift.Unmeasurable)
    assert ei.value.__cause__ is err


def test_git_failure_redacts_credentials(monkeypatch):
    """The credential helper's token can come back in git's own `unable to access` line."""
    err = subprocess.CalledProcessError(128, ["git"], stderr=(
        "fatal: unable to access 'https://x-access-token:ghs_S3cr3tT0ken@github.com/"
        "nousergon/metron.git/': The requested URL returned error: 403\n"
        "hint: token ghp_AnotherSecret was rejected\n"
    ))
    monkeypatch.setattr(deploy_drift.subprocess, "run", _raising_run(err))
    with pytest.raises(GitError) as ei:
        deploy_drift._git("/repo", "fetch", "origin")
    msg = str(ei.value)
    assert "S3cr3t" not in msg and "AnotherSecret" not in msg and "x-access-token" not in msg
    assert "https://***@github.com/nousergon/metron.git/" in msg
    assert "error: 403" in msg


def test_git_failure_keeps_the_tail_of_long_stderr(monkeypatch):
    """git's fatal line comes last, so truncation keeps the end, not the start."""
    noise = "\n".join(f"warning: noise line {i}" for i in range(200))
    err = subprocess.CalledProcessError(128, ["git"], stderr=noise + "\nfatal: the real reason\n")
    monkeypatch.setattr(deploy_drift.subprocess, "run", _raising_run(err))
    with pytest.raises(GitError) as ei:
        deploy_drift._git("/repo", "rev-parse", "HEAD")
    msg = str(ei.value)
    assert msg.endswith("fatal: the real reason")
    assert "noise line 0 " not in msg
    assert len(msg) < deploy_drift.STDERR_TAIL_CHARS + 100


def test_git_failure_with_empty_stderr_says_so(monkeypatch):
    err = subprocess.CalledProcessError(1, ["git"], stderr=None)
    monkeypatch.setattr(deploy_drift.subprocess, "run", _raising_run(err))
    with pytest.raises(GitError, match=r"\(exit 1\): no stderr$"):
        deploy_drift._git("/repo", "rev-parse", "HEAD")


def test_git_timeout_is_a_git_error_with_stderr(monkeypatch):
    """TimeoutExpired carries bytes even under text=True — still decoded and folded in."""
    err = subprocess.TimeoutExpired(["git"], 120, stderr=b"fatal: stalled\n")
    monkeypatch.setattr(deploy_drift.subprocess, "run", _raising_run(err))
    with pytest.raises(GitError, match=r"timed out after 120s: fatal: stalled$"):
        deploy_drift._git("/repo", "ls-remote", "origin", "refs/heads/main")


def test_origin_without_main_is_unmeasurable(monkeypatch):
    _record_git(monkeypatch, {("ls-remote",): ""})
    with pytest.raises(deploy_drift.Unmeasurable, match="origin has no refs/heads/main"):
        deploy_drift.read_state("/repo")


def test_check_names_an_unreadable_repo_and_still_measures_the_rest(monkeypatch, caplog):
    """One flaky remote must not hide real drift on the other repo, and the log line is
    the one box-health's classifier reads."""
    def read(path):
        if path == "/repo/flaky":
            raise GitError("git ls-remote origin refs/heads/main in /repo/flaky failed "
                           "(exit 128): fatal: unable to access")
        return _state(head="old", remote="new", behind=2, age=600)

    monkeypatch.setattr(deploy_drift, "read_state", read)
    with caplog.at_level("ERROR", logger=deploy_drift.__name__):
        out = deploy_drift.check(("/repo/flaky", "/repo/stale"))
    assert [s.behind for s in out.drifted] == [2]
    assert [u.path for u in out.unmeasured] == ["/repo/flaky"]
    assert "fatal: unable to access" in out.unmeasured[0].reason
    assert "deploy drift: could not measure /repo/flaky (git ls-remote" in caplog.text
    assert out.exit_code == deploy_drift.EXIT_DRIFT, "drift outranks could-not-measure"


def test_check_with_only_unreadable_repos_exits_3(monkeypatch):
    def read(path):
        raise GitError("boom")

    monkeypatch.setattr(deploy_drift, "read_state", read)
    out = deploy_drift.check(("/a", "/b"))
    assert out.drifted == [] and len(out.unmeasured) == 2
    assert out.exit_code == deploy_drift.EXIT_UNMEASURABLE == 3


def test_verdict_exit_codes_for_in_sync_and_drift_are_unchanged():
    assert Verdict().exit_code == 0
    assert Verdict(drifted=[_state()]).exit_code == 1


def test_cli_unmeasurable_exits_3_with_its_own_warning_alert(monkeypatch):
    """Not the drift page: its own severity and its own dedup key, so a flaky hour neither
    buzzes the phone nor suppresses a real drift page inside the drift dedup window."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: Verdict(unmeasured=[
        Unmeasured(path="/repo/ops", reason="git ls-remote ... (exit 128): fatal: 403"),
    ]))
    assert deploy_drift.main([]) == 3
    assert len(sent) == 1
    text, kwargs = sent[0]
    assert "could not measure" in text and "/repo/ops" in text and "fatal: 403" in text
    assert kwargs["severity"] == "warning"
    assert kwargs["dedup_key"] == "metron-deploy-drift-unmeasurable"
    assert kwargs["dedup_key"] != "metron-deploy-drift"
    assert kwargs["dry_run"] is False


def test_cli_drift_and_unmeasurable_sends_both_and_exits_1(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: Verdict(
        drifted=[_state(head="old", remote="new", behind=4, age=5760)],
        unmeasured=[Unmeasured(path="/repo/ops", reason="boom")],
    ))
    assert deploy_drift.main([]) == 1
    assert sorted(kw["dedup_key"] for _t, kw in sent) == [
        "metron-deploy-drift", "metron-deploy-drift-unmeasurable",
    ]


def test_dry_run_unmeasurable_exits_3_and_sends_nothing_real(monkeypatch, capsys):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerting, "send_alert", lambda t, **kw: sent.append((t, kw)) or True)
    monkeypatch.setattr(deploy_drift, "check", lambda **kw: Verdict(
        unmeasured=[Unmeasured(path="/repo/ops", reason="boom")],
    ))
    assert deploy_drift.main(["--dry-run"]) == 3
    assert [kw["dry_run"] for _t, kw in sent] == [True]
    assert "could not be measured" in capsys.readouterr().out


def test_against_real_git_an_unreachable_origin_carries_gits_stderr(tmp_path, monkeypatch):
    """The closes-when of metron-ops#288, against git itself: a forced ls-remote failure
    produces could-not-measure, exit 3, and git's own stderr in the reason."""
    box = tmp_path / "box"
    subprocess.run(["git", "init", "--quiet", str(box)], check=True)
    subprocess.run(
        ["git", "-C", str(box), "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    monkeypatch.setattr(deploy_drift.time, "sleep", lambda _s: None)
    out = deploy_drift.check((str(box),))
    assert out.exit_code == 3
    reason = out.unmeasured[0].reason
    assert "ls-remote" in reason and "(exit 128)" in reason
    assert "does not appear to be a git repository" in reason or "missing.git" in reason
