"""Every first-party module must import as the FIRST thing a fresh interpreter loads.

The in-process suite cannot see an import cycle: ``conftest.py`` imports ``api.main``
before any test runs, which loads the whole package in one particular order, and a cycle
only bites a process that enters it from the other side. That is exactly how the
scheduled jobs start — ``python -m api.maintenance <subcommand>`` loads
``api.maintenance`` first, not ``api.main`` — so a top-level import that closes a cycle
can leave the API healthy, every test green, and every maintenance subcommand dying at
import with ``ImportError: cannot import name ... from partially initialized module``.

It shipped once: ``tearsheet``, ``deploy_cash``, ``metrics_enrichment`` and the
Market board router gained a module-level ``demo_household`` import, and
``demo_household`` imports ``goal -> performance -> security_perf -> tearsheet`` at
load. Daily refresh, flex-sync, reconcile and shadow-recompute all failed at import
from then on, while the API (entered through ``api.main``) kept serving.

Each module is imported in a child interpreter with every first-party module evicted
from ``sys.modules`` beforehand, so each one is genuinely the entry point. Third-party
packages stay cached between modules — a cycle inside them is not this repo's to catch,
and re-importing them per module would cost minutes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("api", "portfolio_analytics")

_CHILD = r"""
import importlib, json, sys, traceback
packages = tuple(sys.argv[1].split(","))
failures = {}
for name in sys.argv[2:]:
    for loaded in [m for m in sys.modules if m.split(".")[0] in packages]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except BaseException:  # noqa: BLE001 - report every failure, keep going
        failures[name] = traceback.format_exc(limit=-3).strip().splitlines()[-1]
print(json.dumps(failures))
"""


def _first_party_modules() -> list[str]:
    names = []
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            names.append(".".join(parts))
    return names


def test_module_discovery_sees_the_scheduled_entry_points():
    modules = _first_party_modules()
    # Guard the guard: if discovery silently found nothing, the test below would pass.
    assert "api.maintenance" in modules
    assert "api.services.tearsheet" in modules
    assert len(modules) > 50


def test_every_module_imports_as_a_fresh_entry_point():
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, ",".join(PACKAGES), *_first_party_modules()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    failures = json.loads(result.stdout.strip().splitlines()[-1])
    assert failures == {}, "modules that cannot be imported first:\n" + "\n".join(
        f"  {name}: {error}" for name, error in sorted(failures.items())
    )


def test_maintenance_cli_starts():
    """The systemd units run ``python -m api.maintenance <subcommand>``; this is that
    process up to argument parsing, with nothing else loaded first."""
    result = subprocess.run(
        [sys.executable, "-m", "api.maintenance", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "daily-refresh" in result.stdout
