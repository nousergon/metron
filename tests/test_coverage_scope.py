"""The coverage gate's *scope* is asserted here, not only its number.

repository-baseline-policy.md §4.2 C5: the way a coverage gate stops being
honest is by narrowing what it measures rather than by lowering the number —
which reads as an improvement in every report. Measured on symposion, removing
one flag moved the reported figure from 34.76% to 92.36% with no new test
code.

Metron measures two top-level packages (`portfolio_analytics` — the engine,
`api` — the FastAPI service), unlike a single-package repo, and its floor is
enforced by a CLI flag in `.github/workflows/ci.yml` rather than
`[tool.coverage.report] fail_under` in `pyproject.toml` — `pyproject.toml`
carries no `fail_under` key at all, so a test that looked only there would
find no floor and pass vacuously. So these tests assert what a passing suite
cannot otherwise notice:

* the measured source is BOTH whole packages (C1), never a path or submodule
  narrower than that;
* the floor is enforced by a non-zero exit (C2) via the CI workflow's
  `--cov-fail-under` flag and is a ratchet that may be raised and never
  lowered (C3);
* the `omit` list is pinned to its current, individually-justified members —
  a future PR that widens it silently shrinks the denominator without this
  test noticing the change in words;
* every top-level Python package in the repo (identified by an `__init__.py`)
  is inside the measured source set — nothing is invisible to the gate by
  omission of a different kind.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The floor may be RAISED here as coverage improves. Lowering it is a policy
#: amendment (repository-baseline-policy.md §4.2 C3), not a code change.
MINIMUM_FLOOR = 95

#: Both application packages — the pure engine and the FastAPI service.
EXPECTED_SOURCE = {"portfolio_analytics", "api"}

#: Individually justified in pyproject.toml's [tool.coverage.run] comment
#: (SnapTrade's thin REST client is exercised live, not in units).
EXPECTED_OMIT = {
    "*/__init__.py",
    "portfolio_analytics/broker_io/snaptrade_reader.py",
}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_coverage_source_is_both_whole_packages() -> None:
    """C1 — ``source`` names both packages, so unimported modules still count."""
    source = set(_pyproject()["tool"]["coverage"]["run"]["source"])
    assert source == EXPECTED_SOURCE, (
        f"coverage source must be exactly {sorted(EXPECTED_SOURCE)}, got "
        f"{sorted(source)!r}. Narrowing it to a submodule or a path measures "
        "the tested subset and reports it as the repository."
    )


def test_coverage_floor_is_enforced_in_ci_and_never_lowered() -> None:
    """C2 + C3 — pyproject.toml carries no fail_under; the CI flag is the gate."""
    report = _pyproject()["tool"]["coverage"].get("report", {})
    assert "fail_under" not in report, (
        "pyproject.toml now sets [tool.coverage.report] fail_under directly — "
        "update this test to read the floor from there instead of from "
        f"{CI_WORKFLOW.name}, and keep exactly one enforced floor."
    )

    text = CI_WORKFLOW.read_text(encoding="utf-8")
    found = re.findall(r"--cov-fail-under=(\d+)", text)
    assert len(found) == 1, (
        f"expected exactly one --cov-fail-under in {CI_WORKFLOW.name}, got "
        f"{found!r}"
    )
    assert int(found[0]) >= MINIMUM_FLOOR, (
        f"coverage floor {found[0]} is below the ratchet {MINIMUM_FLOOR}. "
        "A floor is raised as coverage improves and never lowered to make a "
        "change pass (repository-baseline-policy.md §4.2 C3)."
    )


def test_ci_measures_both_packages() -> None:
    """The `--cov` flags actually passed to pytest match the declared source."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    covered = set(re.findall(r"--cov=(\S+)", text))
    assert covered == EXPECTED_SOURCE, (
        f"{CI_WORKFLOW.name} passes --cov for {sorted(covered)!r}, expected "
        f"{sorted(EXPECTED_SOURCE)!r} — a mismatch here means CI's actual "
        "invocation measures something other than what pyproject.toml declares."
    )


def test_coverage_omit_matches_the_pinned_justified_set() -> None:
    """A shrunk denominator is a narrowing this test forces into review."""
    omit = set(_pyproject()["tool"]["coverage"]["run"].get("omit", []))
    added = omit - EXPECTED_OMIT
    removed = EXPECTED_OMIT - omit
    assert not added, (
        f"coverage omit gained unreviewed entries: {sorted(added)}. Each "
        "omitted path removes files from the denominator, raising the "
        "reported figure without adding a test — update EXPECTED_OMIT here "
        "alongside its justification in pyproject.toml if this is deliberate."
    )
    assert not removed, (
        f"coverage omit lost tracked entries: {sorted(removed)}. If these "
        "modules are now measured, good — also narrow EXPECTED_OMIT here."
    )


def test_every_top_level_package_is_inside_the_measured_source() -> None:
    """No application package is invisible to the gate by living outside source."""
    packages = {
        p.parent.name
        for p in REPO_ROOT.glob("*/__init__.py")
    }
    assert packages, "no top-level Python packages found — this check would pass vacuously"
    stray = packages - EXPECTED_SOURCE
    assert not stray, (
        f"top-level packages outside the measured source are invisible to "
        f"the coverage gate: {sorted(stray)}"
    )
