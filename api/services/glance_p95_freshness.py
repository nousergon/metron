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


def check(session, *, now: datetime | None = None) -> dict | None:
    """Return a finding dict when the glance p95 surface is unhealthy, else ``None``.

    Two independent conditions, either of which is a defect on its own:
      - ``latest()["status"] == "stale"`` — a day WAS measured once, but the newest
        measured day has fallen behind the daily cadence (glance_latency._is_stale).
      - the last GlanceP95RunLog entry (any outcome) is older than
        ``_NO_RUN_GRACE_DAYS`` — the job hasn't even ATTEMPTED a run recently, which
        ``latest()``'s "not-measured" status alone cannot distinguish from "never
        configured yet" on a fresh deployment.
    """
    from api.services import glance_latency

    latest = glance_latency.latest(session, now=now)
    reasons: list[str] = []

    if latest["status"] == "stale":
        reasons.append(
            f"the last measured trading day ({latest['trading_day']}) has fallen behind "
            "the daily cadence -- the p95 figure /meta/status serves is stale, not current"
        )

    last_run = latest["last_run"]
    ref = (now or datetime.utcnow())
    if isinstance(ref, datetime):
        ref_date = ref.date()
    else:
        ref_date = ref  # pragma: no cover - now is always a datetime in practice
    if last_run is None:
        reasons.append("scripts/glance_p95.py --record has never run -- no run-log entry exists at all")
    else:
        last_run_date = date.fromisoformat(last_run["at"][:10])
        age_days = (ref_date - last_run_date).days
        if age_days > _NO_RUN_GRACE_DAYS:
            reasons.append(
                f"scripts/glance_p95.py --record last ran {age_days} day(s) ago "
                f"({last_run['at']}, status={last_run['status']}) -- the scheduled timer "
                "appears to have stopped firing"
            )

    if not reasons:
        return None
    return {"reasons": reasons, "latest": latest, "last_run": last_run}


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
        f"Metron: the glance p95 exit-gate measurement (metron-ops-I327/I341) looks broken:\n"
        f"  {detail}\n"
        f"Check metron-glance-p95.timer on the box (journalctl -u metron-glance-p95.service).",
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
