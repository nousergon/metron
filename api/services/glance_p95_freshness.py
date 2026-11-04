"""Dead-man monitor for the glance p95 measurement job (metron-ops-I341).

``shared-application-host-policy.md`` T0-4: "every scheduled job is dead-man-monitored
— a timer that stops firing must alert." The measurement job itself
(``scripts/glance_p95.py --record``, run daily by ``metron-glance-p95.timer``) cannot be
the thing that reports its own absence — a box that is down, a timer that was disabled,
or a crash before the process even starts leaves nothing to piggyback an alert on. So
this is a SEPARATE, independently scheduled check (mirroring ``deploy_drift.py``'s
state-based backstop, not the event it is watching): it reads ``glance_latency.latest()``
and pages when the figure has gone ``"stale"`` or has never been produced at all past a
grace window, regardless of why.

Deliberately state-based, same reasoning as ``deploy_drift.check()``: a red box, a
disabled timer, an SSM outage, or a silent crash inside the measurement job all look
identical from here, which is the point — "detect the missing effect, never the missing
event."
"""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

# How many days GlanceP95RunLog may go without a NEW entry before "the job never ran" is
# a defect rather than a box that hasn't reached its next scheduled fire yet. The job
# runs once daily; one full day of slack covers a late Persistent=true catch-up run after
# a reboot without paging on ordinary timing.
_NO_RUN_GRACE_DAYS = 2

# How many distinct TRADING days the job may report ``not-measured`` before "no day has
# ever been measured" is a finding rather than a fresh deployment (metron-ops-I344). Same
# tolerance as ``glance_latency._FRESHNESS_GRACE_TRADING_DAYS``: one quiet trading day is
# ordinary, two in a row with nothing ever measured is not. Weekends and holidays never
# count, so a quiet Saturday cannot page.
_NEVER_MEASURED_TRADING_DAYS = 2

# Not-measured days BEFORE this target day are not evidence of missing traffic: until
# metron#593 (merged 2026-10-30) the parser was anchored at line start and could not
# match the production log format, so every run read n=0 whatever the traffic
# (alpha-engine-config-I11642). Counting those days made the alert read "33 trading
# days never measured" when 29 of them were the parser bug, not the screen.
_PARSER_FIX_DAY = date(2026, 10, 30)


def check(session, *, now: datetime | None = None) -> dict | None:
    """Return a finding dict when the glance p95 surface is unhealthy, else ``None``.

    Three independent conditions, any of which is a defect on its own:
      - ``latest()["status"] == "stale"`` — a day WAS measured once, but the newest
        measured day has fallen behind the daily cadence (glance_latency._is_stale).
      - no day has EVER been measured, yet the job has run and reported ``not-measured``
        for ``_NEVER_MEASURED_TRADING_DAYS`` or more trading days (metron-ops-I344). Since
        a not-measured run exits 0, this check — not a failed systemd unit — is where a
        run of silent trading days gets graded; without it, a deployment whose glance
        log line never matched would read healthy forever.
      - the last GlanceP95RunLog entry (any outcome) is older than
        ``_NO_RUN_GRACE_DAYS`` — the job hasn't even ATTEMPTED a run recently, which
        ``latest()``'s "not-measured" status alone cannot distinguish from "never
        configured yet" on a fresh deployment.
    """
    from api.services import glance_latency

    latest = glance_latency.latest(session, now=now)
    reasons: list[str] = []
    # "no_traffic": the job works, but no glance request reached the API (every glance
    # request logs a timing line, and since metron#593 the parser matches it).
    # "job_broken": stale figure, or the job has stopped running.
    kind = None

    if latest["status"] == "stale":
        kind = "job_broken"
        reasons.append(
            f"the last measured trading day ({latest['trading_day']}) has fallen behind "
            "the daily cadence -- the p95 figure /meta/status serves is stale, not current"
        )

    if latest["status"] == "not-measured":
        quiet = [d for d in _not_measured_trading_days(session)
                 if d >= _PARSER_FIX_DAY.isoformat()]
        if len(quiet) >= _NEVER_MEASURED_TRADING_DAYS:
            kind = kind or "no_traffic"
            reasons.append(
                f"no glance requests on {len(quiet)} trading day(s) since the log-parser "
                f"fix (metron#593, {_PARSER_FIX_DAY.isoformat()}), latest {quiet[-1]}"
            )

    last_run = latest["last_run"]
    ref = (now or datetime.utcnow())
    if isinstance(ref, datetime):
        ref_date = ref.date()
    else:
        ref_date = ref  # pragma: no cover - now is always a datetime in practice
    if last_run is None:
        kind = "job_broken"
        reasons.append("scripts/glance_p95.py --record has never run -- no run-log entry exists at all")
    else:
        last_run_date = date.fromisoformat(last_run["at"][:10])
        age_days = (ref_date - last_run_date).days
        if age_days > _NO_RUN_GRACE_DAYS:
            kind = "job_broken"
            reasons.append(
                f"scripts/glance_p95.py --record last ran {age_days} day(s) ago "
                f"({last_run['at']}, status={last_run['status']}) -- the scheduled timer "
                "appears to have stopped firing"
            )

    if not reasons:
        return None
    return {"reasons": reasons, "kind": kind, "latest": latest, "last_run": last_run}


def _alert_text(finding: dict) -> str:
    """Issue, why it matters, what fixes it, what Brian does (Brian, 2026-11-04;
    observability-policy § Notification — auto-fix line). Written into the body so it
    renders the same on every krepis version."""
    detail = "; ".join(finding["reasons"])
    if finding.get("kind") == "no_traffic":
        return (
            f"Metron glance p95 has no reading: {detail}.\n"
            "Impact: Metron Stage A exit gate O2 (glance p95 <= 1s) cannot be graded. "
            "Trading and Crucible are unaffected.\n"
            "Auto-fix: none; a reading needs real glance traffic.\n"
            "Resolution: metron-ops-I341.\n"
            "You: open a portfolio's Glance tab at https://metron.nousergon.ai on a "
            "trading day; the next morning's run measures it."
        )
    return (
        f"Metron glance p95 job is broken: {detail}.\n"
        "Impact: the Metron exit-gate figure on /meta/status is missing or stale.\n"
        "Auto-fix: tonight's sweep (7pm PT) root-causes it from the alert intake "
        "(journalctl -u metron-glance-p95.service).\n"
        "Resolution: metron-ops-I341.\n"
        "You: nothing."
    )


def _not_measured_trading_days(session) -> list[str]:
    """Distinct trading days (ISO, ascending) the run log recorded as ``not-measured``.
    Non-trading target days are dropped: an empty Saturday is not evidence of anything."""
    from krepis.trading_calendar import is_trading_day
    from sqlalchemy import select

    from api.db import models

    days = session.scalars(
        select(models.GlanceP95RunLog.target_day)
        .where(models.GlanceP95RunLog.status == "not-measured")
        .distinct()
    ).all()
    return sorted(d.isoformat() for d in days if is_trading_day(d))


def report(session, *, now: datetime | None = None, dry_run: bool = False) -> dict | None:
    """Check and page the operator if unhealthy. Returns the finding (or None).

    ``dry_run`` threads straight through to ``send_alert``/``krepis.alerts.publish``
    (metron-ops-I340 pattern) -- detection still runs for real, only the send is
    suppressed, for exercising the fire path without paging the operator."""
    from api.services.alerting import send_alert

    finding = check(session, now=now)
    if finding is None:
        logger.info("glance p95 freshness check: healthy")
        return None
    detail = "; ".join(finding["reasons"])
    logger.error("glance p95 freshness check FAILED: %s", detail)
    alert_dedup_name = "metron-glance-p95-freshness"
    send_alert(
        _alert_text(finding),
        severity="error",
        dedup_key=alert_dedup_name,
        dedup_window_min=720,  # twice-daily paging ceiling for a condition that persists
        dry_run=dry_run,
    )
    return finding


def main(argv: list[str] | None = None) -> int:
    """``python -m api.services.glance_p95_freshness`` -- the entry point the systemd
    dead-man-monitor timer uses. Exit 0 healthy, 1 unhealthy (alert sent or attempted)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m api.services.glance_p95_freshness",
        description="Dead-man monitor: page when the glance p95 exit-gate figure has gone stale or the measurement job has stopped firing.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="check for real and print the verdict, but suppress the actual alert send",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from api.db.session import SessionLocal

    with SessionLocal() as session:
        finding = report(session, dry_run=args.dry_run)

    if args.dry_run:
        if finding:
            print(f"[dry-run] nothing was sent. unhealthy: {'; '.join(finding['reasons'])}")
        else:
            print("[dry-run] nothing was sent. healthy -- no alert would have been sent.")
    return 1 if finding else 0


if __name__ == "__main__":
    raise SystemExit(main())
