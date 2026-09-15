"""Market board copy-lint (metron-ops-I304): rates the SECURITY, never the holding
(positioning `metron.md` §3d E8) — no buy CTA, no directive wording. Locks the invariant
directly on the shipped files (grep-based, mirrors test_no_advisor_strings.py) rather than
relying on review discipline holding forever.
"""

from __future__ import annotations

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_FILES = [
    _REPO_ROOT / "web" / "components" / "market-board.tsx",
    _REPO_ROOT / "web" / "app" / "portfolios" / "[id]" / "market" / "page.tsx",
    _REPO_ROOT / "api" / "routers" / "market_board.py",
]

# Case-insensitive; word-boundary so "recommendation-worthy" style false positives from
# unrelated compounds don't trip it, but "recommend"/"recommended"/"recommending" all do.
_DIRECTIVE = re.compile(
    r"\b(buy now|sell now|should buy|should sell|you should|we recommend|recommend(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)


def test_no_directive_wording_in_market_board_files():
    offenders = []
    for path in _FILES:
        assert path.exists(), f"expected market-board file missing: {path}"
        content = path.read_text(encoding="utf-8")
        for match in _DIRECTIVE.finditer(content):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}: {match.group()!r}")
    assert offenders == [], f"directive wording found in market-board copy: {offenders}"


def test_no_sort_by_position_weight_field_in_the_row_model():
    """Rate the security, never the holding: MarketBoardRowOut's field set must carry no
    quantity/weight/market_value field an accidental future column could sort by. Scoped
    to the class BODY only (not the whole file's prose, which legitimately discusses
    "position weight" as the thing this router deliberately excludes)."""
    content = (_REPO_ROOT / "api" / "routers" / "market_board.py").read_text(encoding="utf-8")
    match = re.search(r"class MarketBoardRowOut\(BaseModel\):\n(.*?)\n\n\n", content, re.DOTALL)
    assert match, "MarketBoardRowOut class body not found"
    body = match.group(1)
    banned_fields = ("quantity", "market_value", "weight", "position_value", "cost_basis")
    offenders = [f for f in banned_fields if re.search(rf"\b{f}\b", body)]
    assert offenders == [], f"position/weight field(s) found on the market-board row: {offenders}"
