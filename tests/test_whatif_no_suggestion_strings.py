"""What-if reallocation sandbox: zero-suggestion invariant (metron-ops#171).

Pre-registration positioning constraint, not a style preference: the sandbox measures a
hypothetical portfolio the USER typed in — it never generates a weight, never ranks an
alternative, and never uses prescriptive wording. Mirrors the grep-based string-invariant
pattern in ``test_beta_no_yfinance.py`` / ``test_no_advisor_strings.py`` (metron-PR217):
scan the panel's UI-string source for forbidden vocabulary and fail loud if any appears.

Scoped to the what-if panel's own source files (not the whole frontend) — the panel is a
self-contained addition, and other pages/pillars ("Value" factor, "Quality" pillar, etc.)
are out of scope for this lock.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The what-if panel's own source — UI strings + the math it renders live here. Extending
# the sandbox (e.g. a second panel component) must add its file to this list explicitly,
# so the invariant can't silently stop covering new surface.
_WHATIF_FILES = (
    "web/components/holdings-whatif-panel.tsx",
    "web/lib/whatif.ts",
)

# Forbidden vocabulary anywhere in the what-if panel's source: generated proposals,
# ranked alternatives, or prescriptive wording. Case-insensitive, word-bounded so it
# doesn't false-positive on unrelated substrings (e.g. "shoulder").
_FORBIDDEN = re.compile(
    r"\b(optimal|optimize|optimizing|optimization|recommend|recommended|recommendation|"
    r"should|suggest|suggested|suggestion|advis(e|or|ory)|best\s+allocation|ideal\s+weight)\b",
    re.IGNORECASE,
)


def test_whatif_panel_never_suggests_a_weight():
    """Locks the zero-suggestion invariant: no generated weight proposals, no
    "optimal"/"recommended"/"should" wording anywhere in the what-if panel's source —
    covers UI copy, code comments, and identifiers alike, since even a comment that
    frames the feature as advisory would signal the wrong positioning to a future editor."""
    offenders: list[str] = []
    for rel in _WHATIF_FILES:
        path = _REPO_ROOT / rel
        assert path.exists(), f"expected what-if source file missing: {rel}"
        content = path.read_text(encoding="utf-8")
        for match in _FORBIDDEN.finditer(content):
            line_no = content.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line_no}: {match.group()!r}")

    assert offenders == [], (
        f"Zero-suggestion invariant violated — forbidden wording in the what-if panel: {offenders}"
    )
