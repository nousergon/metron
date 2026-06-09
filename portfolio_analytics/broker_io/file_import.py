"""Shared shape for user-uploaded file imports (CSV, OFX, …).

Every file importer reduces a dirty user upload to the same two things: a canonical
``ConnectorSnapshot`` of what parsed, and a per-record log of what didn't. Hoisting
that shape here keeps one summary contract across all import endpoints (the API
renders one ``ImportOut`` regardless of file type) and one posture on bad input:

  * a single un-parseable **record** is collected (``SkippedRecord`` with a human
    ``ref`` + ``reason``), never silently dropped — the user sees exactly what failed;
  * a structurally **unusable file** raises ``FileImportError`` (the whole import is
    invalid, e.g. a missing required column or no recognizable statement).

``ref`` is a human locator scoped to the format — ``"line 4"`` for CSV, ``"fitid
T123"`` for OFX — so the skip log reads naturally whatever the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from portfolio_analytics.ingestion.base import ConnectorSnapshot


class FileImportError(ValueError):
    """The uploaded file is structurally unusable (not just a few bad records)."""


@dataclass
class SkippedRecord:
    """One un-importable record, surfaced to the user verbatim."""

    ref: str       # human locator: "line 4" (CSV) | "fitid T123" (OFX)
    reason: str
    raw: dict = field(default_factory=dict)


@dataclass
class FileImportResult:
    """Outcome of parsing one uploaded file: canonical snapshot + per-record skip log."""

    snapshot: ConnectorSnapshot
    errors: list[SkippedRecord] = field(default_factory=list)
    parsed: int = 0     # records that produced an activity
    skipped: int = 0    # records recorded in ``errors``
