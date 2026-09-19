"""Per-run accounting of what the maintenance commands actually read from the database.

**Why this exists (metron-ops-I343).** ``metron-prod`` egresses a metronomic
0.225-0.236 GB every day, and has done since 2026-08-02. It exceeded its 5 GB
monthly ceiling on 2026-08-22 and finished August at 7.13 GB — ten consecutive
CRITICAL days that were never fixed, only reset by the billing calendar.
September is on the identical trajectory.

The database is **66 MB**, and its compute endpoint is awake only ~72 minutes a
day, in windows that line up exactly with the ``metron-refresh`` /
``metron-reconcile`` / ``metron-refresh-flex`` timers. So the egress is not a
trickle from a long-lived connection — it is roughly **45-50 MB pulled per
scheduled run**, against a 66 MB database. Each run reads most of the database.

Three calls inside ``maintenance.daily_refresh`` are whole-history
re-derivations rather than incremental updates, and are the standing suspects:

* ``performance.reconstruct_snapshots`` — rebuilds the entire NAV series from
  the lot timeline, per portfolio, per run. ``daily-refresh`` fires three times
  a night (20:45 / 21:30 / 22:30), so the same history is rebuilt three times.
* ``performance.reconcile_snapshots`` — restates prior provisional snapshots.
* ``fx.backfill_fx_rates`` — from ``earliest``, the first foreign transaction
  ever, to today, on every run.

**This module does not fix any of that, and deliberately changes no valuation
logic.** It measures, so the fix targets the call that actually dominates
rather than the one that looks guiltiest. ``reconstruct_snapshots`` sits on a
path with four separately-fixed NAV correctness bugs (metron-ops#74, #87, #88,
#89); changing its read pattern on a hypothesis is how a fifth gets written.

**What is measured, and what is NOT.** Rows materialised and wall-clock seconds
per labelled block, both exact. **Bytes are not reported**, because nothing in
the SQLAlchemy/psycopg path exposes wire bytes per statement, and an estimate
derived from row widths would read as a measurement while being a guess. Rows
localise the read; the Neon monitor's ``data_transfer_bytes`` remains the
authority on the total, and the two are compared by moving one and watching the
other.

The artifact lands under ``s3://<market_data_bucket>/metron/db_read_profile/``.
That prefix is already granted: ``alpha-engine-dashboard-role``'s
``alpha-engine-research-access`` policy carries ``s3:PutObject`` on
``arn:aws:s3:::alpha-engine-research/metron/*`` — verified against the live role
2026-09-19, not assumed. No IAM change accompanies this.

Publication is **best-effort and never fatal**: a maintenance run's deliverable
is the refreshed prices and NAV snapshots, and losing a profile must not cost
them. That is a deliberate swallow — the failure mode is "this run's profile is
missing", the primary deliverable survives untouched, and the recording surface
is a WARN in the unit's journal naming the underlying S3 error.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

#: Bumped when a field is added, removed, or changes meaning. A reader keys off
#: this rather than guessing from shape.
SCHEMA_VERSION = 1

#: Everything this module writes lives under here.
KEY_PREFIX = "metron/db_read_profile"


@dataclass
class _Block:
    """One labelled region of a run, aggregated across every entry into it."""

    label: str
    calls: int = 0
    rows: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "calls": self.calls,
            "rows": self.rows,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class ReadProfile:
    """Collects labelled read measurements for one maintenance command.

    Deliberately a plain object passed down rather than a module-level global:
    two commands can run concurrently on the same box (the 20:45 refresh and a
    23:15 reconcile do not overlap today, but nothing enforces that), and a
    global would silently merge their measurements into one wrong artifact.
    """

    command: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    blocks: dict[str, _Block] = field(default_factory=dict)

    @contextmanager
    def block(self, label: str):
        """Time a region and let the body report how many rows it materialised.

        ``yield``s a one-argument callable; the body calls it with a row count.
        A body that never calls it records ``rows=0``, which is honest — the
        region was timed, nothing claimed to be counted — and is why `rows` and
        `seconds` are reported side by side rather than one being derived from
        the other.
        """
        entry = self.blocks.setdefault(label, _Block(label))
        counted = 0

        def record(rows: int) -> None:
            nonlocal counted
            counted += int(rows)

        start = time.monotonic()
        try:
            yield record
        finally:
            # In a `finally` so a raising body still contributes its cost. A
            # block that blew up after reading 40k rows read them regardless,
            # and dropping that is how the expensive call hides in the profile.
            entry.calls += 1
            entry.rows += counted
            entry.seconds += time.monotonic() - start

    def as_dict(self) -> dict:
        ordered = sorted(self.blocks.values(), key=lambda b: b.seconds, reverse=True)
        return {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "total_rows": sum(b.rows for b in ordered),
            "total_seconds": round(sum(b.seconds for b in ordered), 3),
            "blocks": [b.as_dict() for b in ordered],
        }

    def log_summary(self) -> None:
        """One line per block, loudest first, into the unit's journal.

        Present so the measurement is readable on the box without S3 — the
        artifact is for trending, this is for the operator reading
        ``journalctl`` five minutes after a slow run.
        """
        payload = self.as_dict()
        logger.info(
            "db-read-profile %s: %d rows in %.1fs across %d blocks",
            payload["command"], payload["total_rows"],
            payload["total_seconds"], len(payload["blocks"]),
        )
        for b in payload["blocks"]:
            logger.info(
                "  %-34s calls=%-4d rows=%-9d %.1fs",
                b["label"], b["calls"], b["rows"], b["seconds"],
            )


def publish(profile: ReadProfile, *, bucket: str, s3_client=None, now: datetime | None = None) -> str | None:
    """Write the profile to S3 and return its key, or None if it could not be written.

    Two objects per run: a dated one for the series, and ``latest.json`` for a
    reader that wants the most recent without listing. Best-effort by contract —
    see the module docstring.
    """
    profile.log_summary()
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H-%M-%SZ")
    dated = f"{KEY_PREFIX}/{profile.command}/{stamp}.json"
    latest = f"{KEY_PREFIX}/{profile.command}/latest.json"
    body = json.dumps(profile.as_dict(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        if s3_client is None:
            import boto3

            s3_client = boto3.client("s3")
        for key in (dated, latest):
            s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    except Exception as e:  # noqa: BLE001 — see the module docstring's swallow rationale
        logger.warning(
            "db-read-profile: could not publish to s3://%s/%s (%s) — the run's own "
            "deliverables are unaffected; this run has no profile artifact",
            bucket, dated, e,
        )
        return None
    logger.info("db-read-profile: published s3://%s/%s", bucket, dated)
    return dated
