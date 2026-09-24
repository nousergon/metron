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

**`ls-remote` retries on failure, and that is load-bearing too.** At 09:07 UTC on
2026-09-12 the hourly run's `ls-remote` died with exit 128 — a single failed TCP round
trip to GitHub, nothing to do with the box or the deploy — and the unit went CRITICAL,
then self-resolved an hour later when the next run's `ls-remote` simply worked. This
check asks the remote once an hour; treating one failed round trip as a red unit pages
for network noise, not drift. `remote_head` now retries the read three times with a
short backoff before letting the error propagate, so a real outage still reddens the
unit — only the transient case is absorbed. Nothing else retries: `rev-parse` reads the
box's own state and a retry there would hide a real local-git problem, and the fetch
path already has its own retry loop in `deploy.yml`.

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

**Could-not-measure is its own outcome, not drift and not a crash** (metron-ops#288,
#291). On 2026-09-06 and again on 2026-09-09 a failed ``ls-remote`` left the journal
with nothing but ``CalledProcessError ... exit status 128`` — git's stderr was captured
and thrown away, so auth, DNS, rate limit and a transient 5xx all read identically, and
box-health paged ``timer job failing`` exactly as it would for a deploy that had not
landed. Now every failed git call raises ``GitError`` carrying argv, exit code and git's
own stderr tail (URL userinfo and token shapes redacted — the credential helper's token
can appear in a ``fatal: unable to access`` line), and a repo whose state cannot be read
is reported as ``deploy drift: could not measure <repo> (<reason>)`` with a
``severity=warning`` alert under its own dedup key. It still reddens the unit: a check
that cannot read the state must never report "no drift".

**Exit codes** (the systemd unit and box-health's driver classifier key on these):

- ``0`` — every repo measured, none drifted.
- ``1`` — drift: at least one repo is behind origin/main past the grace window. Drift
  wins over could-not-measure when both happen in one run, because it is the stronger,
  paged finding; the could-not-measure warning is still sent.
- ``3`` — could not measure: no drift was found, but at least one repo's state could not
  be read (a git call failed, or origin has no ``refs/heads/main``).

Anything else escaping ``main()`` is an unhandled crash and exits ``1`` with a traceback,
as Python does; ``2`` is argparse's usage error.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How long a commit may sit on origin/main before the box running something older is a
# defect rather than a deploy in progress. A green metron deploy takes ~100s end to end;
# 30 minutes leaves room for the flock wait (up to 10m) plus a retry without paging.
DEFAULT_GRACE_MINUTES = 30

# Both repos the box serves from — a merge to EITHER triggers a deploy of BOTH, so either
# one lagging means deploys are not landing.
REPOS = ("/home/ec2-user/metron", "/home/ec2-user/metron-ops")

# Exit codes — documented in the module docstring; keep the two in step.
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNMEASURABLE = 3


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


class Unmeasurable(RuntimeError):
    """This repo's deployed-vs-remote position could not be read. Not drift, not "in
    sync" — ``check()`` reports it as its own outcome (exit 3)."""


class GitError(Unmeasurable):
    """A git call failed. The message carries argv, exit code and git's own stderr tail,
    because ``exit status 128`` alone names nothing (metron-ops#291)."""


# git's fatal line is the last one, so keep the tail. Long enough for a proxy or TLS
# error, short enough to stay one readable journal line and one alert.
STDERR_TAIL_CHARS = 400

# `https://x-access-token:<token>@github.com/...` is exactly what a credential-helper
# failure can echo back. Redact the userinfo of any URL, and GitHub token shapes anywhere.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]+")


def _redact(text: str) -> str:
    text = _URL_USERINFO.sub(r"\g<scheme>***@", text)
    return _GITHUB_TOKEN.sub("***", text)


def _stderr_tail(stderr: str | bytes | None) -> str:
    """git's stderr as one redacted, truncated line (the journal is read line by line)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    flat = " | ".join(line.strip() for line in (stderr or "").splitlines() if line.strip())
    flat = _redact(flat)
    if len(flat) > STDERR_TAIL_CHARS:
        flat = "…" + flat[-STDERR_TAIL_CHARS:]
    return flat or "no stderr"


def _git(path: str, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, check=True, timeout=120
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise GitError(
            f"git {' '.join(args)} in {path} failed (exit {exc.returncode}): "
            f"{_stderr_tail(exc.stderr)}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {' '.join(args)} in {path} timed out after {exc.timeout:g}s: "
            f"{_stderr_tail(exc.stderr)}"
        ) from exc


# Where this check parks the remote tip when it needs history. A ref nothing else on the
# box reads or writes, so a deploy fetching into refs/remotes/origin/* at the same instant
# contends with nothing. See the module docstring for the outage that named it.
DRIFT_REF = "refs/deploy-drift/main"


# ls-remote is a network round trip to GitHub; retried before it reddens the unit. See
# the module docstring for the 2026-09-12 09:07 UTC exit-128 that motivated this.
REMOTE_HEAD_ATTEMPTS = 3
REMOTE_HEAD_BACKOFF_SEC = (2.0, 4.0)


def remote_head(path: str, *, sleep=time.sleep) -> str:
    """Full SHA at ``origin/main``, read without writing a single ref.

    ``ls-remote`` asks the remote and prints; it updates no ref, no ``FETCH_HEAD``, and
    takes no lock. That makes the overwhelmingly common case — box is current, nothing to
    report — entirely side-effect-free, which is what a check running every hour against a
    live deploy target should always have been.

    Retried up to ``REMOTE_HEAD_ATTEMPTS`` times on a git failure — a transient network
    blip talking to GitHub, not deploy drift — before the ``GitError`` propagates and the
    run reports could-not-measure. Only this remote read retries; ``rev-parse`` (local,
    never flaky) and the fetch path (its own retry loop lives in ``deploy.yml``) do not.
    """
    last_exc: Exception | None = None
    for attempt in range(1, REMOTE_HEAD_ATTEMPTS + 1):
        try:
            out = _git(path, "ls-remote", "origin", "refs/heads/main")
            return out.split()[0] if out else ""
        except GitError as exc:
            last_exc = exc
            if attempt == REMOTE_HEAD_ATTEMPTS:
                raise
            logger.warning(
                "ls-remote failed for %s (attempt %d/%d), retrying: %s",
                path, attempt, REMOTE_HEAD_ATTEMPTS, exc,
            )
            sleep(REMOTE_HEAD_BACKOFF_SEC[attempt - 1])
    raise last_exc  # pragma: no cover — loop always returns or raises above


def read_state(path: str) -> RepoState:
    """Read this repo's deployed-vs-remote position. Raises ``Unmeasurable`` (a
    ``GitError`` for a failed git call) — a check that cannot read the state must not
    report 'no drift'."""
    remote_full = remote_head(path)
    if not remote_full:
        # Not "no drift". A remote with no main is a broken premise, and the honest
        # response is a red unit, not a green one.
        raise Unmeasurable("origin has no refs/heads/main")

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


@dataclass(frozen=True)
class Unmeasured:
    path: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    drifted: list[RepoState] = field(default_factory=list)
    unmeasured: list[Unmeasured] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.drifted:
            return EXIT_DRIFT
        if self.unmeasured:
            return EXIT_UNMEASURABLE
        return EXIT_OK


def check(
    repos: tuple[str, ...] = REPOS, *, grace_minutes: int = DEFAULT_GRACE_MINUTES
) -> Verdict:
    """Report every repo whose deployed code is behind origin/main past the grace window,
    and every repo whose position could not be read at all.

    One unreadable repo does not stop the others being measured — a flaky remote for
    metron-ops must not hide real drift on metron. Only ``Unmeasurable`` is caught; any
    other exception is a bug and propagates as a crash. Alerting and the exit code are
    the caller's (``report``/``main``).
    """
    drifted: list[RepoState] = []
    unmeasured: list[Unmeasured] = []
    for path in repos:
        try:
            state = read_state(path)
        except Unmeasurable as exc:
            logger.error("deploy drift: could not measure %s (%s)", path, exc)
            unmeasured.append(Unmeasured(path=path, reason=str(exc)))
            continue
        if is_drifted(state, grace_minutes=grace_minutes):
            drifted.append(state)
    for s in drifted:
        logger.error(
            "deploy drift: %s is running %s but origin/main is %s (%d commit(s) behind, "
            "newest waiting %d min) — a deploy has not landed, and the services still "
            "serving the OLD code will keep health-checking green",
            s.path, s.head, s.remote, s.behind, s.remote_age_min,
        )
    return Verdict(drifted=drifted, unmeasured=unmeasured)


def report(*, grace_minutes: int = DEFAULT_GRACE_MINUTES, dry_run: bool = False) -> Verdict:
    """Check for drift and page the operator if there is any. Returns the verdict.

    Deduped over 6 hours: the timer runs hourly, and a stuck deploy should page a few
    times a day rather than 24.

    Could-not-measure sends a SEPARATE ``severity=warning`` alert under its own dedup
    key (metron-ops#288): it is not a deploy that failed to land, so it must neither
    borrow the drift page nor share its dedup window — a flaky hour must not suppress
    the drift page that follows it.

    ``dry_run`` threads straight to ``send_alert``/``krepis.alerts.publish`` (metron-ops-
    I340): drift is still detected for real and nothing about dedup key, window, or
    severity changes — only the send is suppressed. This is what `--dry-run` uses to
    exercise the path without paging; forcing real drift against the live box to prove
    the alert fires is the anti-pattern this exists to avoid (see
    `external_demo_release_gate.py`'s module docstring for the incident that motivated
    it in this module's sibling check).
    """
    from api.services.alerting import send_alert

    verdict = check(grace_minutes=grace_minutes)
    if verdict.unmeasured:
        detail = "\n".join(f"  - {u.path}: {u.reason}" for u in verdict.unmeasured)
        send_alert(
            f"Metron: the deploy-drift check could not measure the box — this is NOT "
            f"drift, and nothing is known either way about these repos:\n{detail}",
            severity="warning",
            dedup_key="metron-deploy-drift-unmeasurable",
            dedup_window_min=360,
            dry_run=dry_run,
        )
    drifted = verdict.drifted
    if not drifted:
        if not verdict.unmeasured:
            logger.info("deploy-drift check: box is at origin/main for every repo")
        return verdict
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
        dry_run=dry_run,
    )
    return verdict


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

    ``--dry-run`` still checks for real and still exits non-zero on drift or
    could-not-measure — it only suppresses the send, so the fire path can be verified
    without paging the operator (metron-ops-I340). Exit codes: see the module docstring.
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
    parser.add_argument(
        "--dry-run", action="store_true",
        help="check for real and print the verdict as usual, but suppress the actual "
             "alert send (nothing reaches SNS/Telegram) — for verifying the fire path, "
             "not a live check",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    verdict = report(grace_minutes=args.grace_minutes, dry_run=args.dry_run)
    if args.dry_run:
        if verdict.drifted:
            print(
                f"[dry-run] nothing was sent. {len(verdict.drifted)} repo(s) drifted — a "
                f"real run in this state would have sent a severity=error alert."
            )
        if verdict.unmeasured:
            print(
                f"[dry-run] nothing was sent. {len(verdict.unmeasured)} repo(s) could not "
                f"be measured — a real run would have sent a severity=warning alert."
            )
        if not verdict.drifted and not verdict.unmeasured:
            print("[dry-run] nothing was sent. no drift detected — a real run sends nothing here either.")
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
