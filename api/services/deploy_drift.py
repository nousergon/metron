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


def read_state(path: str) -> RepoState:
    """Fetch and read this repo's deployed-vs-remote position. Raises on a git failure —
    a check that cannot read the state must not report 'no drift'."""
    _git(path, "fetch", "origin", "--quiet")
    head = _git(path, "rev-parse", "--short", "HEAD")
    remote = _git(path, "rev-parse", "--short", "origin/main")
    behind = int(_git(path, "rev-list", "--count", "HEAD..origin/main") or 0)
    age_min = 0
    if behind:
        committed_at = int(_git(path, "log", "-1", "--format=%ct", "origin/main"))
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
