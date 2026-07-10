"""Intelligence rename: no user-facing 'Advisor' strings remain (metron-ops#165).

The pre-registration surface must not self-describe as an advisor. This test locks
the invariant: user-facing copy has been renamed to 'Intelligence', internal code
identities (type names, API endpoints, package names) can stay as 'advisor'.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Grep patterns for "Advisor" string literals in user-facing code.
# Allow 'advisor' in: type names (AdvisorProfile, AdvisorView), API paths (/ext/advisor),
# package/function names (getAdvisor, generateAdvisor), and internal identifiers.
# DENY in: UI strings, error messages, comments about user-facing text.
_USER_FACING_ADVISOR = re.compile(
    r"""
    (?:
        # JSX/HTML strings: "... Advisor ...", 'The Advisor', &apos;t available
        ["\'](?:[^"\']*\bAdvisor\b[^"\']*)["\'] |
        # Template literals: `The Advisor isn't`
        `(?:[^`]*\bAdvisor\b[^`]*)` |
        # Error/feedback text in .txt/.md comments
        (?<!/)//.*\b(?:AI\s+)?Advisor\b(?!.*type|function|router|API) |
        # HTML entities like &apos; wrapped around Advisor
        &apos;?(?:AI\s+)?Advisor
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

# Allowlist: specific files/patterns where 'advisor' is legitimate.
_ALLOWLIST = {
    # Component/function names, type definitions (code identity) — OK.
    "web/components/advisor-profile-form.tsx",
    "web/components/generate-advisor.tsx",
    "web/__tests__/advisor-profile-form.test.tsx",
    "web/app/portfolios/[id]/advisor/page.tsx",
    "web/app/portfolios/[id]/advisor/profile/page.tsx",
    "web/app/portfolios/[id]/advisor/profile/actions.ts",
    "web/app/portfolios/[id]/advisor/actions.ts",
    "web/lib/api.ts",  # Type defs, function names (getAdvisor, AdvisorView, etc.)
    # Comments in code referencing the old naming or explaining the data structure
    "api/routers/research_intel.py",
}


def test_user_facing_code_never_says_advisor():
    """Locks the pre-registration invariant: UI strings say 'Intelligence', not 'Advisor'.
    Internal code (type names, API routes, function names) can keep 'advisor'. This mirrors
    the test_app_code_never_imports_yfinance pattern: grep-based invariant lock."""
    offenders = []

    # Check web frontend (user-facing copy + tests).
    for path in (_REPO_ROOT / "web").rglob("*.tsx"):
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        rel = str(path.relative_to(_REPO_ROOT))
        if rel in _ALLOWLIST:
            continue

        content = path.read_text(encoding="utf-8")
        # Only check JSX/HTML strings and comments in user-facing contexts.
        for match in _USER_FACING_ADVISOR.finditer(content):
            offenders.append(f"{rel}: {match.group()[:60]}")

    assert offenders == [], (
        f"User-facing 'Advisor' strings found (should be 'Intelligence'): {offenders}"
    )
