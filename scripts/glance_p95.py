#!/usr/bin/env python3
"""Compute the glance aggregate endpoint's p95 latency for one trading day
(metron-ops-I327, Stage A exit gate O2: ``GET /portfolios/{id}/glance`` p95 <= 1.0 s
over a trading day) and optionally record the result so ``/meta/status`` can quote it.

This script does not know where this deployment's logs live (deploy topology is
private, `metron-ops`-owned per this repo's AGENTS.md) — it reads already day-scoped
log TEXT from stdin or ``--log-file`` and leaves fetching that text to the caller.
Every request logs one ``glance composed`` (success) or ``glance failed`` (error) line
carrying ``duration_ms=`` (``api/routers/glance.py``); this script sums nothing else.

Usage, once a day's worth of the app's log output is available:

    # from a plain log file (journald export, a captured stdout log, ...):
    python scripts/glance_p95.py --date 2026-09-16 --log-file glance_2026-09-16.log --record

    # piped from wherever the deployment's log sink is queried from, e.g. (illustrative
    # only — the actual log-group / stream naming is a metron-ops deploy-topology fact,
    # not committed here):
    #   aws logs filter-log-events --filter-pattern '"glance composed" "glance failed"' \\
    #       --start-time <day start epoch ms> --end-time <day end epoch ms> \\
    #       --query 'events[].message' --output text
    aws logs filter-log-events ... | python scripts/glance_p95.py --date 2026-09-16

Prints one JSON object: ``{"trading_day", "p95_ms", "n", "status"}``. ``status`` is
``"not-measured"`` (``p95_ms`` null) when zero timing lines matched — never rendered as
a passing zero. ``--record`` persists a measured result into ``GlanceLatencyDaily``
(refuses to persist a not-measured day: recording nothing IS the not-measured state).
Exit code 1 when ``--record`` was requested but the day was not-measured.

``--record`` ALSO appends one row to ``GlanceP95RunLog`` (metron-ops-I341) for every
outcome — measured, not-measured, or a crash — before this process exits. That is the
EXECUTION record: it is what lets a caller (``/meta/status``) tell "the scheduled job
never ran" apart from "it ran and found nothing to measure", which look identical from
``GlanceLatencyDaily`` alone (a not-measured day is never given a row there, by design).
A failure while reading input or computing the day's figure still gets logged as an
``"error"`` run before the exception propagates — this script never swallows a failure
to keep the run log clean; a clean-looking log on a day that actually crashed would be
worse than a visibly errored one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path


def _read_lines(log_file: str | None) -> list[str]:
    if log_file:
        return Path(log_file).read_text().splitlines()
    return sys.stdin.read().splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="the trading day the input lines are scoped to (YYYY-MM-DD)")
    parser.add_argument("--log-file", default=None, help="read log lines from this file instead of stdin")
    parser.add_argument("--record", action="store_true", help="persist a measured result into GlanceLatencyDaily")
    args = parser.parse_args(argv)

    try:
        trading_day = date.fromisoformat(args.date)
    except ValueError:
        parser.error(f"--date must be YYYY-MM-DD, got {args.date!r}")
        return 2  # pragma: no cover - argparse.error already exits

    # Imported lazily so `--help` works without a configured DATABASE_URL.
    from api.services import glance_latency

    if not args.record:
        # Dry-run path (no DB touched, no run log): used to inspect a day's figure
        # ad hoc, never by the scheduled unit.
        lines = _read_lines(args.log_file)
        result = glance_latency.compute(lines, trading_day=trading_day)
        payload = {
            "trading_day": result.trading_day.isoformat(),
            "p95_ms": result.p95_ms,
            "n": result.n,
            "status": result.status,
        }
        print(json.dumps(payload))
        return 0

    # --record path: EVERY outcome — measured, not-measured, or a crash — appends one
    # GlanceP95RunLog row before this process exits (metron-ops-I341). That row is the
    # execution signal a freshness check reads; a swallowed crash here would look
    # identical to a healthy quiet day from every surface downstream.
    from api.db.session import SessionLocal

    with SessionLocal() as session:
        try:
            lines = _read_lines(args.log_file)
            result = glance_latency.compute(lines, trading_day=trading_day)
        except Exception as exc:
            glance_latency.record_run(session, target_day=trading_day, status="error", error=str(exc))
            raise

        payload = {
            "trading_day": result.trading_day.isoformat(),
            "p95_ms": result.p95_ms,
            "n": result.n,
            "status": result.status,
        }

        if result.p95_ms is None:
            glance_latency.record_run(session, target_day=trading_day, status="not-measured", n=result.n)
            print(json.dumps(payload))
            print(f"not-measured: 0 matching lines for {trading_day} — refusing to record.", file=sys.stderr)
            return 1

        try:
            glance_latency.record(session, result)
        except Exception as exc:
            glance_latency.record_run(
                session, target_day=trading_day, status="error", n=result.n, error=str(exc)
            )
            raise
        glance_latency.record_run(
            session, target_day=trading_day, status="measured", n=result.n, p95_ms=result.p95_ms,
        )
        payload["recorded_at"] = datetime.utcnow().isoformat() + "Z"

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
