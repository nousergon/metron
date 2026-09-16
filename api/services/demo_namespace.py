"""The reserved ``DEMO-`` symbol namespace — one definition, shared by every demo seeder.

``securities`` and ``price_bars`` are GLOBAL, cross-tenant tables (see their docstrings
in ``api/db/models.py``): one row per symbol, read by every tenant holding that symbol.
A demo fixture that writes a synthetic close or an overwritten name/asset_class under a
REAL ticker therefore injects fake reference data into every real tenant's
TWR / risk / shadow-recompute / market-board series for that symbol.

That is not hypothetical — it shipped twice:

  * ``demo_household`` (metron-PR464) nearly seeded a 5-year synthetic price history
    under bare ``AAPL``/``MSFT``/… ; caught in review, fixed before merge by moving the
    whole fixture under this prefix.
  * ``demo``'s Showcase sample sleeve DID ship it (metron-ops-I319): a frozen
    ``VOO = 490.0`` close on 2024-06-28 plus ``name``/``asset_class`` overwrites on the
    real global ``VOO`` / ``912828YK0`` / ``VMFXX`` rows, live in production from the
    sleeve's introduction until that issue's fix.

Second adoption, so the guard lives here rather than in two copies
(``nous-ergon-ops/policies/shared-code-policy.md``): ``api/services/demo.py`` and
``api/services/demo_household.py`` both import it, and ``api/maintenance.py`` uses
:func:`is_demo_symbol` to keep the namespace out of every live vendor fetch.

The guards RAISE. A demo symbol without the prefix is a fixture-authoring bug that has
to be fixed at the fixture, never a case to skip quietly — a silent skip here is exactly
what would let the next instance of this defect reach production unobserved.
"""

from __future__ import annotations

from collections.abc import Iterable

# Every symbol any demo seeder writes to ``securities`` / ``price_bars`` starts with
# this. Real tickers never do: the prefix is not a legal listing symbol on any exchange
# Metron ingests, and no broker/CSV path constructs one (the importer passes the
# broker's own symbol through verbatim), so a ``DEMO-``-prefixed Security row can only
# ever have been created by a demo seeder in this repo.
DEMO_SYMBOL_PREFIX = "DEMO-"


def is_demo_symbol(symbol: str | None) -> bool:
    """True when ``symbol`` is inside the reserved demo namespace."""
    return bool(symbol) and symbol.startswith(DEMO_SYMBOL_PREFIX)


def assert_demo_symbols(symbols: Iterable[str | None], *, context: str) -> None:
    """Raise ``ValueError`` if ANY symbol is outside the reserved demo namespace.

    Call this at every demo write site that touches the global ``securities`` or
    ``price_bars`` tables, immediately before the write — ``context`` names the call
    site so the traceback points at the fixture to fix.
    """
    offenders = sorted({s or "" for s in symbols if not is_demo_symbol(s)})
    if offenders:
        raise ValueError(
            f"{context}: refusing to write demo data for non-namespaced symbol(s) "
            f"{offenders!r} — every demo fixture symbol must start with "
            f"{DEMO_SYMBOL_PREFIX!r}, because securities/price_bars are GLOBAL, "
            f"cross-tenant tables a real tenant's holding shares (metron-ops-I319)"
        )
