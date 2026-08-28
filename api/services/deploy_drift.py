"""Alert when the box is running code older than `origin/main` (metron-ops#268).

**Why this exists.** `deploy.yml` failed on every run for four days (2026-08-02 →
2026-08-03) and nothing said so. The failure mode is quiet by construction: the deploy
script dies before `systemctl restart`, so the PREVIOUSLY deployed services keep serving
and keep answering `:8000/health` with a 200. Health of the running process is not
evidence that the deploy landed — the only honest question is *which commit is running*.

This check answers that question from the box's own state, so it does not care WHY a
deploy did not land: a red workflow, a workflow that never fired, an SSM outage, a failed
alert, a hand-reverted checkout — all of them look identical here, which is the point.
The fleet lesson it applies: **detect the missing effect, never the missing event.**

A grace window keeps an in-flight deploy from paging: drift is only reported once the
newest commit on `origin/main` has been sitting there longer than ``grace_minutes``.

**This check never writes ``refs/remotes/origin/*``, and that is load-bearing.** It used
to open with a plain ``git fetch origin`` in the deployed working copy — the same working
copy ``deploy.yml`` fetches into over SSM. On 2026-08-27 20:07 UTC the two collided: the
hourly timer fires at ``*:07`` and the deploy for 95cd989 landed in the same second, so
the deploy died with

    error: cannot lock ref 'refs/remotes/origin/main': is at 95cd989 but expected 0f2a6b8

before ``deploy-on-merge.sh`` ever started — which also meant the deploy script's own
failure trap never ran. The commit stayed undeployed for five hours and this check
faithfully reported the drift **it had itself caused**. A monitor that mutates the state
it observes is not a monitor; it is a second writer with an alerting side effect.

So the healthy path is now completely write-free (``git ls-remote``, which touches no ref
and no ``FETCH_HEAD``), and the drifted path — the only one that needs history — fetches
into the private ref ``refs/deploy-drift/main`` with ``--no-write-fetch-head``. Neither
takes a lock any other process contends for. Objects are shared, which git already
handles concurrently.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How long a commit may sit on origin/main before the box running something older is a
# defect rather than a deploy in progress. A green metron deploy takes ~100s end to end;
# 30 minutes leaves room for the flock wait (up to 10m) plus a retry without paging.
DEFAULT_GRACE_MINUTES = 30

# Both repos the box serves from — a merge to EITHER triggers a deploy of BOTH, so either
# one lagging means deploys are not landing.
REPOS = ("/home/ec2-user/metron", "/home/ec2-user/metron-ops")


@dataclass(frozen=True)
class RepoState:
    path: str
    head: str            # short SHA checked out on the box
    remote: str          # short SHA at origin/main
    remote_age_min: int  # minutes since the origin/main commit was authored
    behind: int          # commits between head and origin/main


def is_drifted(state: RepoState, *, grace_minutes: int = DEFAULT_GRACE_MINUTES) -> bool:
    """True when this repo is running code that should already have been deployed.

    Deliberately NOT `head != remote`: a box legitimately sits at a different SHA for the
    couple of minutes a deploy takes, and paging on that would make the check noise. It is
    also not `behind > N` — one undeployed commit past the grace window is exactly the
    condition, and a threshold above 1 would have stayed silent through the four-day
    outage that motivated this (only four commits landed in that window).
    """
    if state.head == state.remote:
        return False
    if state.behind <= 0:
        # Ahead of, or diverged from, origin/main — a hand-edited box. Not what this
        # check is for, and reporting it as "deploys are broken" would be wrong.
        return False
    return state.remote_age_min > grace_minutes


def _git(path: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", path, *args], capture_output=True, text=True, check=True, timeout=120
    ).stdout.strip()


# Where this check parks the remote tip when it needs history. A ref nothing else on the
# box reads or writes, so a deploy fetching into refs/remotes/origin/* at the same instant
# contends with nothing. See the module docstring for the outage that named it.
DRIFT_REF = "refs/deploy-drift/main"


def remote_head(path: str) -> str:
    """Full SHA at ``origin/main``, read without writing a single ref.

    ``ls-remote`` asks the remote and prints; it updates no ref, no ``FETCH_HEAD``, and
    takes no lock. That makes the overwhelmingly common case — box is current, nothing to
    report — entirely side-effect-free, which is what a check running every hour against a
    live deploy target should always have been.
    """
    out = _git(path, "ls-remote", "origin", "refs/heads/main")
    return out.split()[0] if out else ""


def read_state(path: str) -> RepoState:
    """Read this repo's deployed-vs-remote position. Raises on a git failure — a check
    that cannot read the state must not report 'no drift'."""
    remote_full = remote_head(path)
    if not remote_full:
        # Not "no drift". A remote with no main is a broken premise, and the honest
        # response is a red unit, not a green one.
        raise RuntimeError(f"{path}: origin has no refs/heads/main")

    head_full = _git(path, "rev-parse", "HEAD")
    head = _git(path, "rev-parse", "--short", "HEAD")
    if head_full == remote_full:
        # Current. Nothing fetched, nothing written, nothing to compute.
        return RepoState(path=path, head=head, remote=head, remote_age_min=0, behind=0)

    # Behind, ahead, or diverged — all three need the remote commit locally to say which.
    #
    # Three flags, each load-bearing, and the third is the one that is easy to miss:
    #   --no-write-fetch-head  FETCH_HEAD is the other file two concurrent fetches lock.
    #   --force                the private ref is a scratch pointer, not history.
    #   --refmap=              WITHOUT THIS THE FIX DOES NOT WORK. Given an explicit
    #                          refspec, git STILL applies the remote's configured refmap
    #                          "opportunistically" and updates refs/remotes/origin/main
    #                          anyway — which is the exact write that broke the deploy.
    #                          An empty --refmap turns that off. Caught by the real-git
    #                          test below, which failed on the first version of this fix.
    _git(
        path, "fetch", "--quiet", "--no-write-fetch-head", "--force", "--refmap=",
        "origin", f"refs/heads/main:{DRIFT_REF}",
    )
    remote = _git(path, "rev-parse", "--short", DRIFT_REF)
    behind = int(_git(path, "rev-list", "--count", f"HEAD..{DRIFT_REF}") or 0)
    age_min = 0
    if behind:
        committed_at = int(_git(path, "log", "-1", "--format=%ct", DRIFT_REF))
        age_min = max(0, int((time.time() - committed_at) // 60))
    return RepoState(path=path, head=head, remote=remote, remote_age_min=age_min, behind=behind)


def check(
    repos: tuple[str, ...] = REPOS, *, grace_minutes: int = DEFAULT_GRACE_MINUTES
) -> list[RepoState]:
    """Report every repo whose deployed code is behind origin/main past the grace window.

    Returns the drifted repos (empty = healthy). Alerting and the exit code are the
    caller's (``api.maintenance``), matching how the broker-staleness check is wired.
    """
    drifted = [s for s in (read_state(p) for p in repos) if is_drifted(s, grace_minutes=grace_minutes)]
    for s in drifted:
        logger.error(
            "deploy drift: %s is running %s but origin/main is %s (%d commit(s) behind, "
            "newest waiting %d min) — a deploy has not landed, and the services still "
            "serving the OLD code will keep health-checking green",
            s.path, s.head, s.remote, s.behind, s.remote_age_min,
        )
    return drifted


def report(*, grace_minutes: int = DEFAULT_GRACE_MINUTES) -> list[RepoState]:
    """Check for drift and page the operator if there is any. Returns the drifted repos.

    Deduped over 6 hours: the timer runs hourly, and a stuck deploy should page a few
    times a day rather than 24.
    """
    from api.services.alerting import send_alert

    drifted = check(grace_minutes=grace_minutes)
    if not drifted:
        logger.info("deploy-drift check: box is at origin/main for every repo")
        return []
    detail = "\n".join(
        f"  - {s.path}: running {s.head}, origin/main is {s.remote} "
        f"({s.behind} commit(s) behind, newest waiting {s.remote_age_min} min)"
        for s in drifted
    )
    send_alert(
        f"Metron: the box is running code behind origin/main — a deploy has not landed:\n"
        f"{detail}\nServices still serving the OLD code will keep health-checking green; "
        f"check the deploy.yml run history.",
        severity="error",
        dedup_key="metron-deploy-drift",
        dedup_window_min=360,
    )
    return drifted


def main(argv: list[str] | None = None) -> int:
    """`python -m api.services.deploy_drift` — the entry point the systemd unit uses.

    Deliberately NOT a subcommand of ``api.maintenance``. That module imports
    ``api.db.models`` at module load, which builds the engine, so the drift check
    inherited a hard dependency on the database being configured — and on 2026-08-03 the
    new SQLite guard (metron-ops#264) turned that inherited dependency into a crash: the
    unit loads only the repo-root env file, which still names SQLite, so
    `python -m api.maintenance deploy-drift-check` died at import before checking
    anything. The unit's own comment claimed it was DB-free "so it keeps reporting when
    the database is the thing that is broken"; that was aspiration, not architecture.
    This module imports nothing from api.db, which makes the claim true.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m api.services.deploy_drift",
        description="Alert when the box is running code behind origin/main.",
    )
    parser.add_argument(
        "--grace-minutes", type=int, default=DEFAULT_GRACE_MINUTES,
        help="how long a commit may sit on origin/main before an undeployed box is a "
             f"defect rather than a deploy in progress (default: {DEFAULT_GRACE_MINUTES})",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return 1 if report(grace_minutes=args.grace_minutes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
