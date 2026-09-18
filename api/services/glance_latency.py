"""The glance aggregate endpoint's p95 latency (metron-ops-I327, Stage A exit gate O2:
``GET /portfolios/{id}/glance`` p95 <= 1.0 s server-side over a trading day).

Four parts, one per deliverable:

1. The per-request timing RECORD is the router's own log line (``api.routers.glance`` —
   ``glance composed`` on success, ``glance failed`` on error) — the durable structured
   log every other module in this app already writes to via the standard ``logging``
   module. This file does not emit a second one.
2. ``p95`` / ``parse_line`` / ``compute`` turn a stream of those log lines into a p95
   figure for one trading day — the math ``scripts/glance_p95.py`` runs. Deliberately
   deploy-topology-agnostic (this repo is public — no log-group name, no hostname): the
   script is handed already-day-scoped log text from wherever the deployment's log sink
   is (documented in the script, not hardcoded here).
3. ``record`` / ``latest`` persist and read back that RESULT (not per-request rows) in
   ``GlanceLatencyDaily``, so ``/meta/status`` can quote a number without anyone running
   an ad-hoc query. No row for a day == that day is genuinely unmeasured; ``latest``
   returns that as an explicit ``"not-measured"`` status, never a zero or a green.
4. ``record_run`` / ``last_run`` append the EXECUTION record (metron-ops-I341) — one row
   per invocation of ``scripts/glance_p95.py --record``, regardless of outcome. This is
   what makes "the scheduled timer never fired" distinguishable from "it fired and found
   no glance traffic that day": both produce no new ``GlanceLatencyDaily`` row, but only
   the first produces no new ``GlanceP95RunLog`` row either. ``latest`` folds this into a
   ``"stale"`` status when the newest measured day has fallen behind the daily cadence —
   a stale figure must never be presented as today's (metron-ops-I341 constraint).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from krepis.trading_calendar import last_closed_trading_day, previous_trading_day
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models

# Matches both the router's success and failure timing lines — each carries
# ``duration_ms=<float>`` regardless of outcome, so a request that 500'd still counts
# toward latency (an error is not "fast"; excluding it would understate the gate).
_DURATION_RE = re.compile(r"^glance (?:composed|failed) .*\bduration_ms=([0-9.]+)")


def parse_line(line: str) -> float | None:
    """The ``duration_ms`` value from one glance timing log line, or ``None`` for a line
    that doesn't match (a different log line interleaved in the same stream)."""
    m = _DURATION_RE.search(line)
    if m is None:
        return None
    try:
        return float(m.group(1))
    except ValueError:  # pragma: no cover - the regex only captures digits/dot
        return None


def p95(durations_ms: list[float]) -> float | None:
    """The 95th percentile via nearest-rank (ceil), matching how the exit gate is
    stated ("p95 <= 1.0 s") — no interpolation to argue about. ``None`` for an empty
    input: the empty case is not-measured, never zero."""
    if not durations_ms:
        return None
    ordered = sorted(durations_ms)
    n = len(ordered)
    rank = max(1, -(-n * 95 // 100))  # ceil(n * 0.95), floor-division negation trick
    return ordered[min(rank, n) - 1]


@dataclass
class P95Result:
    trading_day: date
    p95_ms: float | None
    n: int

    @property
    def status(self) -> str:
        return "measured" if self.p95_ms is not None else "not-measured"


def compute(lines: list[str], *, trading_day: date) -> P95Result:
    """``P95Result`` for ``trading_day`` from a caller-supplied, already day-scoped batch
    of log lines (the caller's log query is what scopes the day — this function trusts
    it and does not re-derive a timestamp from log text)."""
    durations = [d for line in lines if (d := parse_line(line)) is not None]
    return P95Result(trading_day=trading_day, p95_ms=p95(durations), n=len(durations))


def record(session: Session, result: P95Result) -> models.GlanceLatencyDaily:
    """Upsert ``result`` into ``GlanceLatencyDaily`` (one row per trading day). Raises
    (never swallows) when ``result.p95_ms`` is ``None`` — a not-measured day is recorded
    nowhere until it IS measured; writing a null/zero row would be exactly the
    green-by-absence failure principle 7 forbids."""
    if result.p95_ms is None:
        raise ValueError(f"refusing to record a not-measured day ({result.trading_day}); n={result.n}")
    row = session.scalar(select(models.GlanceLatencyDaily).where(models.GlanceLatencyDaily.trading_day == result.trading_day))
    if row is None:
        row = models.GlanceLatencyDaily(trading_day=result.trading_day)
        session.add(row)
    row.p95_ms = result.p95_ms
    row.n = result.n
    row.computed_at = datetime.utcnow()
    session.commit()
    return row


def record_run(
    session: Session,
    *,
    target_day: date,
    status: str,
    n: int | None = None,
    p95_ms: float | None = None,
    error: str | None = None,
) -> models.GlanceP95RunLog:
    """Append the EXECUTION record for one ``scripts/glance_p95.py --record`` invocation
    (metron-ops-I341). Always insert, never upsert — each invocation is its own fact,
    unlike ``record`` which upserts one row per trading day. Called for every outcome
    (``"measured"``, ``"not-measured"``, ``"error"``), which is what lets ``latest`` tell
    "the timer never fired" apart from "it fired and found nothing to measure"."""
    if status not in ("measured", "not-measured", "error"):
        raise ValueError(f"status must be measured/not-measured/error, got {status!r}")
    row = models.GlanceP95RunLog(
        target_day=target_day, status=status, n=n, p95_ms=p95_ms, error=error,
    )
    session.add(row)
    session.commit()
    return row


def last_run(session: Session) -> dict | None:
    """The most recent ``GlanceP95RunLog`` entry, or ``None`` if the job has never run
    (not even a not-measured or error attempt) — the raw execution signal ``latest``'s
    freshness check is built on."""
    row = session.scalar(
        select(models.GlanceP95RunLog).order_by(models.GlanceP95RunLog.ran_at.desc()).limit(1)
    )
    if row is None:
        return None
    return {
        "at": row.ran_at.isoformat() + "Z",
        "target_day": row.target_day.isoformat(),
        "status": row.status,
        "error": row.error,
    }


# How many trading days a measured row may lag the last closed session before it reads
# as stale rather than current. The schedule's period is one trading day (the timer runs
# once nightly after close), so one full day of grace covers the normal same-evening lag
# without ever tolerating two consecutive skipped runs unnoticed.
_FRESHNESS_GRACE_TRADING_DAYS = 1


def _is_stale(trading_day: date, *, now: datetime | None = None) -> bool:
    """True when ``trading_day`` has fallen more than ``_FRESHNESS_GRACE_TRADING_DAYS``
    trading days behind the last closed NYSE session — i.e. the scheduled job has missed
    at least one full cadence period. A row for the last closed session, or the one
    before it (same-evening lag), still reads current."""
    lcd = last_closed_trading_day(now)
    threshold = lcd
    for _ in range(_FRESHNESS_GRACE_TRADING_DAYS):
        threshold = previous_trading_day(threshold)
    return trading_day < threshold


def latest(session: Session, *, now: datetime | None = None) -> dict:
    """The most recently computed day's figure for ``/meta/status``.

    ``status`` is one of:
      - ``"not-measured"`` — no day has EVER been recorded (never a zero, never green).
      - ``"stale"`` — a day WAS recorded, but it has fallen more than one cadence period
        (``_FRESHNESS_GRACE_TRADING_DAYS`` trading days) behind the last closed session —
        the scheduled job has missed at least one run. The last known figure is still
        returned (for context) but must never be read as today's number.
      - ``"measured"`` — a day was recorded and it is within the cadence's grace window.

    ``last_run`` carries the most recent ``GlanceP95RunLog`` entry (metron-ops-I341),
    independent of whether that run produced a measured day — this is what distinguishes
    a scheduled job that ran and found no traffic from one that never ran at all."""
    row = session.scalar(select(models.GlanceLatencyDaily).order_by(models.GlanceLatencyDaily.trading_day.desc()).limit(1))
    run = last_run(session)
    if row is None:
        return {
            "status": "not-measured", "trading_day": None, "p95_ms": None, "n": None,
            "target_ms": 1000.0, "stale": False, "last_run": run,
        }
    stale = _is_stale(row.trading_day, now=now)
    return {
        "status": "stale" if stale else "measured",
        "trading_day": row.trading_day.isoformat(),
        "p95_ms": float(row.p95_ms),
        "n": row.n,
        "target_ms": 1000.0,
        "within_target": float(row.p95_ms) <= 1000.0,
        "stale": stale,
        "last_run": run,
    }
