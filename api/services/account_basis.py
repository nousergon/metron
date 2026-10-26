"""Account-basis inputs from the persisted per-account snapshots (metron-ops#371, slice 3).

The ``account_performance.v1`` engine (``portfolio_analytics.domain.account_performance``)
is pure: it takes :class:`WealthObservation` values and account events. This module is
the one place a stored ``AccountNavSnapshot`` row becomes an observation, so the mapping
rules live in one reviewed spot rather than in each caller:

* ``holdings_mv`` is the row's ``nav``: priced holdings only, which is exactly the sum of
  the composition legs.
* ``cash`` and ``cash_source`` come from the row. A row written before the cash columns
  existed, or for an account with no synced balance, carries NULL, and that stays
  ``None`` (unobserved). It is never read as zero.
* ``positions`` is the composition legs' quantity, summed by ticker. A row with no
  composition, or a composition schema this module does not know, has unobserved
  positions, and the engine's position gate then makes the period unavailable.
* Accrued income and financing debit are not stored on the row. The caller declares
  them; the default is ``None`` (unobserved), so a caller that has no source for them
  gets an unavailable period, not a silent zero.

Not in this slice: the ``?basis=account`` route and the account-event feed from the
ledger. Both follow once enough nights of cash have accrued to compute a period.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from portfolio_analytics.domain.account_performance import WealthObservation

COMPOSITION_SCHEMA = 1  # the schema ``performance._composition`` writes


def positions_from_composition(composition: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Quantity by ticker from a snapshot's composition, or ``None`` when the row's
    positions were not observed (no composition, or an unknown schema)."""
    if not composition or composition.get("schema") != COMPOSITION_SCHEMA:
        return None
    legs = composition.get("legs")
    if not isinstance(legs, list):
        return None
    out: dict[str, float] = {}
    for leg in legs:
        ticker, qty = leg.get("ticker"), leg.get("qty")
        if not ticker or qty is None:
            return None  # a leg we cannot read: the positions are not fully observed
        out[ticker] = out.get(ticker, 0.0) + float(qty)
    return out


def observation_from_account_snapshot(
    row: Any,
    *,
    accrued_income: float | None = None,
    financing_debit: float | None = None,
) -> WealthObservation:
    """One ``AccountNavSnapshot`` row as an account-basis :class:`WealthObservation`."""
    cash = getattr(row, "cash_usd", None)
    return WealthObservation(
        when=row.snap_date,
        holdings_mv=float(row.nav),
        cash=float(cash) if cash is not None else None,
        cash_source=getattr(row, "cash_source", None) if cash is not None else None,
        accrued_income=accrued_income,
        financing_debit=financing_debit,
        positions=positions_from_composition(getattr(row, "composition", None)),
    )
