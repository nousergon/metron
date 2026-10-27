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

Slice 3b (:func:`account_basis_performance`) serves it: ``GET /portfolios/{id}/performance
?basis=account`` runs the engine per account over the stored rows and the account's ledger
events. It is opt-in and changes nothing on the default response. Two inputs the row does
not carry are DECLARED, in :data:`CONVENTIONS`, rather than defaulted silently:

* **Income is recognised on a cash basis.** Accrued-but-unpaid income is not modelled, so
  it is not a wealth component (``accrued_income = 0`` by that declared convention, not
  because a value was missing).
* **A margin debit is carried in the cash balance** as a negative balance, both for the
  connector balance and for ledger-accrued cash, so there is no separate financing debit.

Provisional rows (a fund leg still on a stale NAV) are not final valuations and are left
out; the period then spans to the next final row, which the engine handles. Not in this
slice: the portfolio-level rollup across accounts, and the methodology page labels.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from portfolio_analytics.domain.account_performance import (
    BASIS_ACCOUNT,
    CONTRACT,
    AccountEvent,
    Metric,
    WealthObservation,
    account_performance,
    from_transaction,
)

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


# The declared conventions behind every account-basis figure this module serves. They are
# part of the response so a reader never has to infer what a number assumed.
CONVENTIONS: dict[str, str] = {
    "income_recognition": (
        "cash basis: income counts when it is paid; accrued-but-unpaid income is not "
        "modelled and is not a wealth component"
    ),
    "financing": (
        "a margin debit is carried as a negative cash balance; no separate accrued "
        "financing is modelled"
    ),
    "cash_source": (
        "per account and never mixed inside a period: custodian = the connector balance at "
        "its last sync; ledger = cash accrued from the account's transactions"
    ),
    "external_capital": (
        "deposits, withdrawals and transfers in or out only; buys, sells, dividend "
        "reinvestment, dividends, interest and fees are internal"
    ),
    "provisional_snapshots": "excluded: a row with a stale fund NAV is not a final valuation",
    "twr": "daily-linked between final snapshots; external flows at the start of the day",
}
CASH_BASIS_ACCRUED_INCOME = 0.0
FINANCING_IN_CASH = 0.0


@dataclass(frozen=True)
class AccountBasisAccount:
    """One account's ``account_performance.v1`` result, or the reason there is none."""

    account_id: uuid.UUID
    n_snapshots: int  # final rows used
    n_provisional_excluded: int
    first_date: date | None
    last_date: date | None
    periods_reconciled: int = 0
    periods_unreconciled: int = 0
    periods_unavailable: int = 0
    unavailable_reason: str | None = None
    metrics: list[Metric] = field(default_factory=list)


@dataclass(frozen=True)
class AccountBasisSummary:
    accounts: list[AccountBasisAccount]
    contract: str = CONTRACT
    basis: str = BASIS_ACCOUNT
    conventions: dict[str, str] = field(default_factory=lambda: dict(CONVENTIONS))


def _account_result(
    account_id: uuid.UUID, rows: list[Any], events: list[AccountEvent]
) -> AccountBasisAccount:
    final = [r for r in rows if not r.provisional]
    excluded = len(rows) - len(final)
    first = final[0].snap_date if final else None
    last = final[-1].snap_date if final else None
    if len(final) < 2:
        return AccountBasisAccount(
            account_id, len(final), excluded, first, last,
            unavailable_reason=(
                f"{len(final)} final account snapshot(s); the account basis needs two"
            ),
        )
    observations = [
        observation_from_account_snapshot(
            r, accrued_income=CASH_BASIS_ACCRUED_INCOME, financing_debit=FINANCING_IN_CASH
        )
        for r in final
    ]
    result = account_performance(observations, events)
    status = [p.status for p in result.periods]
    return AccountBasisAccount(
        account_id, len(final), excluded, first, last,
        periods_reconciled=status.count("reconciled"),
        periods_unreconciled=status.count("unreconciled"),
        periods_unavailable=status.count("unavailable"),
        metrics=list(result.metrics.values()),
    )


def account_basis_performance(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    account_ids: Collection[uuid.UUID] | None = None,
) -> AccountBasisSummary:
    """``account_performance.v1`` for each account in scope (the selection, or every
    account in the portfolio), from its stored per-account snapshots and its ledger
    events. An account that cannot be computed is listed with its reason, never dropped."""
    acct_q = select(models.Account.id).where(
        models.Account.tenant_id == tenant_id, models.Account.portfolio_id == portfolio_id
    )
    if account_ids is not None:
        acct_q = acct_q.where(models.Account.id.in_(account_ids))
    scope = sorted(session.scalars(acct_q), key=str)
    if not scope:
        return AccountBasisSummary(accounts=[])
    rows_by_account: dict[uuid.UUID, list[Any]] = {a: [] for a in scope}
    for row in session.scalars(
        select(models.AccountNavSnapshot)
        .where(
            models.AccountNavSnapshot.tenant_id == tenant_id,
            models.AccountNavSnapshot.portfolio_id == portfolio_id,
            models.AccountNavSnapshot.account_id.in_(scope),
        )
        .order_by(models.AccountNavSnapshot.snap_date)
    ):
        rows_by_account[row.account_id].append(row)
    events_by_account: dict[uuid.UUID, list[AccountEvent]] = {a: [] for a in scope}
    for account_id, txn in analytics.engine_transactions_by_account(
        session, tenant_id, portfolio_id, account_ids=scope
    ):
        events_by_account.setdefault(account_id, []).append(from_transaction(txn))
    return AccountBasisSummary(
        accounts=[_account_result(a, rows_by_account[a], events_by_account[a]) for a in scope]
    )
