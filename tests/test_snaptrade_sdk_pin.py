"""The SnapTrade SDK major version is load-bearing, so CI holds it (metron-ops#260).

``snaptrade-python-sdk`` 12.x does not produce an accepted request signature for this
deploy's key. Measured on the box 2026-08-03 against the SAME credentials: 11.0.213
returns the six linked accounts; 12.0.3 and 12.0.4 both return
``401 {'detail': 'Authentication credentials were not provided.'}`` in every documented
v12 auth mode. The unbounded ``>=11.0.198`` floor let pip resolve 12.0.1 on a routine
deploy, and Dependabot's major bump (#354) then codified ``>=12.0.3`` — merged green,
because every test in this suite stubs the client, so the bump's only real exercise was
production. Four accounts' share counts froze for nine days.

Nothing in a unit suite can sign a real request, so this is the honest guard: it makes
the *next* attempt to move the major fail here rather than in production. When SnapTrade
documents a working v12 personal-key flow, this test is the thing that must be updated
deliberately — that is the point.
"""

from __future__ import annotations

import pathlib
import re

import snaptrade_client

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _requirement() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'"(snaptrade-python-sdk[^"]*)"', text)
    assert match, "snaptrade-python-sdk requirement not found in pyproject.toml"
    return match.group(1)


def test_requirement_carries_an_upper_bound_below_12():
    """A floor alone is not a pin: pip installs the newest match, so `>=11.0.198` and
    `>=12.0.3` resolve to the same broken 12.x wheel."""
    assert "<12" in _requirement(), (
        f"snaptrade-python-sdk requirement is {_requirement()!r} — the `<12` bound was "
        "removed. 12.x sends unsigned requests for this deploy's key (metron-ops#260); "
        "re-verify against the live credentials before raising it."
    )


def test_installed_sdk_is_the_11_line():
    """Guards the environment the tests actually ran against, not just the declared
    requirement — a stale venv or a resolver override would otherwise pass silently."""
    major = int(snaptrade_client.__version__.split(".")[0])
    assert major == 11, (
        f"snaptrade_client {snaptrade_client.__version__} is installed; the 12.x line "
        "fails authentication for this deploy's key (metron-ops#260)."
    )
