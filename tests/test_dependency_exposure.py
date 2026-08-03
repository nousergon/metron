"""Integration-only dependencies must be bounded and un-auto-bumpable (metron-ops#265).

Some dependencies are stubbed in every test — correctly, because hermetic tests must not
depend on live credentials. The cost is that a version bump's only real exercise is
production. `snaptrade-python-sdk` 12.x reached the box exactly that way: a Dependabot
major, green CI, auto-merged under the standing exception, and SnapTrade position sync was
dead for nine days before a human noticed a stale date on a screen (metron-ops#260).

`[tool.metron.dependency-exposure].integration-only` in pyproject.toml names those
packages. This test enforces what being on that list means:

1. the package is actually a declared dependency (a stale name protects nothing);
2. it carries an upper version bound, so a major cannot arrive on a routine
   `pip install -e .` during a deploy;
3. Dependabot is told not to propose its major, so no green-CI auto-merge can carry
   through a change CI is structurally unable to evaluate.

Requirement 3 is the one that would have stopped metron-PR354.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

_UPPER_BOUND = re.compile(r"[<!~]=?|==")


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _integration_only() -> list[str]:
    return _pyproject()["tool"]["metron"]["dependency-exposure"]["integration-only"]


def _requirement_for(name: str) -> str | None:
    for req in _pyproject()["project"]["dependencies"]:
        # "pkg[extra]>=1,<2" → compare on the bare name before any extra or specifier.
        bare = re.split(r"[\[<>=!~ ]", req, maxsplit=1)[0]
        if bare == name:
            return req
    return None


def _pip_major_ignores() -> set[str]:
    cfg = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    for block in cfg["updates"]:
        if block["package-ecosystem"] != "pip":
            continue
        return {
            entry["dependency-name"]
            for entry in block.get("ignore", [])
            if "version-update:semver-major" in entry.get("update-types", [])
        }
    return set()


def test_the_list_is_not_empty():
    """A vacuous list would make every assertion below pass while enforcing nothing."""
    assert _integration_only(), "[tool.metron.dependency-exposure].integration-only is empty"


def test_each_is_a_real_declared_dependency():
    missing = [n for n in _integration_only() if _requirement_for(n) is None]
    assert not missing, (
        f"listed as integration-only but not declared in [project].dependencies: {missing}. "
        "A name that matches nothing protects nothing."
    )


def test_each_carries_an_upper_bound():
    unbounded = [
        n for n in _integration_only()
        if not _UPPER_BOUND.search(_requirement_for(n) or "")
    ]
    assert not unbounded, (
        f"integration-only dependencies with no upper bound: {unbounded}. pip installs the "
        "newest match, so a bare `>=` floor lets a major arrive on a routine deploy — the "
        "snaptrade-python-sdk 12.x failure mode (metron-ops#260)."
    )


def test_each_has_a_dependabot_major_ignore():
    ignores = _pip_major_ignores()
    unguarded = [n for n in _integration_only() if n not in ignores]
    assert not unguarded, (
        f"integration-only dependencies Dependabot may still major-bump: {unguarded}. "
        "Add a `version-update:semver-major` ignore in .github/dependabot.yml — a green CI "
        "run is not evidence for a package CI cannot exercise."
    )
