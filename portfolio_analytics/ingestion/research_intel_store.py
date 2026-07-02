"""Last-good persistence for the global ``research_intel`` snapshot.

research_intel is GLOBAL, read-only market intel (not per-account holdings), so it does
not belong in the connector silver store (which is keyed by account/security). It gets
its own tiny last-good cache: one JSON blob under the gitignored ``cache/`` dir (which
survives deploys, per ``ingestion.store``). The daily refresh syncs S3 → this cache; the
read-only API reads the cache (fast, and an S3 outage keeps the last-good intel visible
rather than blanking the surface).

Fail-soft on both ends: a fetch error is never persisted (we keep last-good), and a
missing/corrupt cache file reads back as ``None`` (the API then reports ``stale``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from portfolio_analytics.ingestion.research_intel_connector import (
    ResearchIntelConnector,
    ResearchIntelSnapshot,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
RESEARCH_INTEL_PATH = CACHE_DIR / "research_intel.json"


def save_research_intel(snapshot: ResearchIntelSnapshot, *, path: Path | None = None) -> bool:
    """Persist ``snapshot`` as the new last-good, unless it is an error/empty snapshot.

    Returns ``True`` when the cache was updated, ``False`` when the snapshot was skipped
    (fetch error or empty) so the prior last-good is preserved."""
    if snapshot.error is not None or snapshot.is_empty:
        return False
    dest = path or RESEARCH_INTEL_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    tmp.replace(dest)  # atomic swap so a reader never sees a half-written file
    return True


def load_research_intel(*, path: Path | None = None) -> ResearchIntelSnapshot | None:
    """Read the last-good snapshot, or ``None`` when absent/corrupt (fail-soft)."""
    src = path or RESEARCH_INTEL_PATH
    if not src.exists():
        return None
    try:
        return ResearchIntelSnapshot.from_dict(json.loads(src.read_text()))
    except Exception as e:  # noqa: BLE001 — a corrupt cache degrades to "no intel", never crashes
        logger.warning("research_intel cache unreadable at %s: %s", src, e)
        return None


def sync_research_intel(
    connector: ResearchIntelConnector | None = None, *, path: Path | None = None
) -> bool:
    """Fetch the latest artifact and persist it as last-good.

    Best-effort: a fetch failure keeps the prior last-good and returns ``False``. Returns
    ``True`` only when a fresh, non-empty snapshot replaced the cache."""
    conn = connector or ResearchIntelConnector()
    return save_research_intel(conn.sync(), path=path)
