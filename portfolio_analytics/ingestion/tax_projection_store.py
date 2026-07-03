"""Last-good persistence for the telos ``TaxProjection`` artifact.

The tax projection is produced OUTSIDE Metron by the telos tax engine
(``python -m telos.planning`` → ``tax_projection.json``, schema
``nousergon/telos`` ``contracts/tax_projection.schema.json``, version 1.x).
Metron consumes the versioned JSON artifact and NEVER imports telos code —
the artifact IS the contract (M0 discipline; metron-ops#133).

Same last-good shape as ``research_intel_store``: one JSON blob under the
gitignored ``cache/`` dir (survives deploys), written atomically by whatever
sync delivers it (operator copy or the ops refresh), read fail-soft by the
API — a missing/corrupt file reads back as ``None`` and the page shows an
explicit empty state, never a 500.

Schema validation is deliberately split: this store answers "is there a
readable JSON dict at the path?"; the router checks ``schema_version`` so an
unknown major version surfaces as a NAMED error on the page (fail loud)
rather than a silently mis-rendered panel.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path("cache")
TAX_PROJECTION_PATH = CACHE_DIR / "tax_projection.json"

SUPPORTED_SCHEMA_MAJOR = 1


def save_tax_projection(projection: dict, *, path: Path | None = None) -> None:
    """Persist ``projection`` atomically (tmp + replace) as the new last-good."""
    dest = path or TAX_PROJECTION_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(projection, indent=2, sort_keys=True))
    tmp.replace(dest)  # atomic swap so a reader never sees a half-written file


def load_tax_projection(*, path: Path | None = None) -> dict | None:
    """Read the last-good projection dict, or ``None`` when absent/corrupt."""
    src = path or TAX_PROJECTION_PATH
    if not src.exists():
        return None
    try:
        raw = json.loads(src.read_text())
    except Exception as e:  # noqa: BLE001 — a corrupt cache degrades to "no projection"
        logger.warning("tax_projection cache unreadable at %s: %s", src, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("tax_projection cache at %s is not a JSON object", src)
        return None
    return raw


def schema_error(projection: dict) -> str | None:
    """A human-readable error when the artifact's schema major is unsupported.

    telos versions the contract semver-style (``schema_version: "1.0.0"``);
    additive minor bumps pass through, an unknown MAJOR (or a missing/garbled
    version) is refused by name so the page shows exactly what went wrong.
    """
    version = str(projection.get("schema_version", ""))
    major = version.split(".", 1)[0]
    if not major.isdigit():
        return f"tax_projection artifact has no parseable schema_version ({version!r})"
    if int(major) != SUPPORTED_SCHEMA_MAJOR:
        return (
            f"tax_projection schema_version {version} is unsupported "
            f"(this build reads major {SUPPORTED_SCHEMA_MAJOR}.x) — update Metron "
            f"or re-emit the artifact with a compatible telos version"
        )
    return None
