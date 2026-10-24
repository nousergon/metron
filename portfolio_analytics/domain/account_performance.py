"""Full-account performance engine, ``account_performance.v1`` (metron-ops#371, slice 1).

The holdings-sleeve figures in ``api.services.performance`` value priced holdings only
and treat net purchases as the flow. That is right for the sleeve and wrong for an
account. This module is the *account* basis. It is pure, stdlib plus the published
quant core, and it changes no API. Slice 3 persists it behind ``?basis=account``.

The contract, from the design on metron-ops#371:

* **Wealth.** ``W = holdings MV + cash + accrued income − financing debit``. An
  unobserved component is ``None`` and makes the wealth unavailable. It is never
  read as zero.
* **External capital is the account boundary only:** deposits, withdrawals and
  security transfers in or out (at transfer-date MV). Buys, sells, reinvestment,
  dividends, interest, fees, financing and corporate-action cash are internal.
* **P&L decomposition, per period:**
  ``W_end − W_start − external = price P&L + income + corporate-action cash
  − explicit costs + FX translation``. The residual of that identity is the
  reconciliation gate. Above the tolerance the period is ``unreconciled``, and its
  net figures are unavailable with that reason rather than rendered as numbers.
* **Gross** excludes explicit costs and **net** includes them.
* **TWR** is daily-linked, with flows at the start of the day. **MWR** is XIRR over
  external flows only.
* Every metric carries ``basis``, ``units``, ``start``, ``end``, ``n``,
  ``uncertainty`` and ``unavailable_reason``.

Not in this slice: FX translation is accepted as an input amount but nothing derives
it yet (the FX fixture waits on metron-ops#370's as-of FX). Factor residual alpha is
slice 4. The NE-book consumer is slice 5. The canonical ledger has no transfer-in-kind
member yet, so ``from_transaction`` never produces one. A transfer can only come from
a caller that knows it is one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from nousergon_lib.quant.returns import CashFlow, xirr

from portfolio_analytics.domain.ledger import Transaction, TxnType

CONTRACT = "account_performance.v1"
BASIS_ACCOUNT = "account"
DEFAULT_TOLERANCE = 0.01  # one cent
UNRECONCILED = "unreconciled"


class EventKind(StrEnum):
    """The account-basis event vocabulary. It is wider than ``TxnType`` on purpose:
    transfers in kind, financing charges, corporate-action cash and FX translation have
    no canonical ledger member yet, and the engine must be able to say what they are."""

    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER_IN = "TRANSFER_IN"  # securities in, valued at transfer-date MV
    TRANSFER_OUT = "TRANSFER_OUT"
    BUY = "BUY"  # includes dividend REINVESTMENT: internal on this basis
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    FEE = "FEE"
    FINANCING = "FINANCING"  # financing interest paid in cash
    CORPORATE_ACTION_CASH = "CORPORATE_ACTION_CASH"  # e.g. cash in lieu
    FX_TRANSLATION = "FX_TRANSLATION"
    SPLIT = "SPLIT"  # P&L-neutral


EXTERNAL_KINDS: frozenset[EventKind] = frozenset(
    {EventKind.DEPOSIT, EventKind.WITHDRAWAL, EventKind.TRANSFER_IN, EventKind.TRANSFER_OUT}
)


@dataclass(frozen=True)
class AccountEvent:
    """One dated account event. ``amount`` is a positive magnitude whose direction the
    kind sets. For BUY/SELL it is the gross trade value (quantity × price) and ``fees``
    is the commission. For a transfer it is the transfer-date market value."""

    when: date
    kind: EventKind
    amount: float = 0.0
    fees: float = 0.0


_TXN_KIND: dict[TxnType, EventKind] = {
    TxnType.BUY: EventKind.BUY,
    TxnType.REINVESTMENT: EventKind.BUY,
    TxnType.SELL: EventKind.SELL,
    TxnType.DIVIDEND: EventKind.DIVIDEND,
    TxnType.INTEREST: EventKind.INTEREST,
    TxnType.DEPOSIT: EventKind.DEPOSIT,
    TxnType.WITHDRAWAL: EventKind.WITHDRAWAL,
    TxnType.FEE: EventKind.FEE,
    TxnType.SPLIT: EventKind.SPLIT,
}


def from_transaction(txn: Transaction) -> AccountEvent:
    """Map a canonical ledger transaction onto the account-basis vocabulary."""
    kind = _TXN_KIND[txn.type]
    if kind in (EventKind.BUY, EventKind.SELL):
        return AccountEvent(txn.when, kind, amount=txn.quantity * txn.price, fees=txn.fees)
    if kind is EventKind.SPLIT:
        return AccountEvent(txn.when, kind)
    return AccountEvent(txn.when, kind, amount=txn.amount, fees=txn.fees)


def account_external_flow(event: AccountEvent) -> float:
    """External capital on the ACCOUNT basis, from the account's side: ``> 0`` in,
    ``< 0`` out, ``0`` for every internal event. A reinvested dividend is a BUY and
    therefore never external here, unlike on the holdings sleeve."""
    if event.kind in (EventKind.DEPOSIT, EventKind.TRANSFER_IN):
        return event.amount
    if event.kind in (EventKind.WITHDRAWAL, EventKind.TRANSFER_OUT):
        return -event.amount
    return 0.0


@dataclass(frozen=True)
class WealthObservation:
    """Account components observed at one date's close. ``None`` means not observed."""

    when: date
    holdings_mv: float
    cash: float | None
    cash_source: str | None = None  # "custodian" | "ledger"
    accrued_income: float | None = 0.0
    financing_debit: float | None = 0.0

    @property
    def unavailable_reason(self) -> str | None:
        missing = [
            name
            for name, value in (
                ("cash", self.cash),
                ("accrued_income", self.accrued_income),
                ("financing_debit", self.financing_debit),
            )
            if value is None
        ]
        return f"{', '.join(missing)} not observed on {self.when.isoformat()}" if missing else None

    @property
    def wealth(self) -> float | None:
        if self.unavailable_reason is not None:
            return None
        assert self.cash is not None and self.accrued_income is not None
        assert self.financing_debit is not None
        return self.holdings_mv + self.cash + self.accrued_income - self.financing_debit


@dataclass(frozen=True)
class PeriodPnL:
    """One period's decomposition. Monetary fields are in account currency (USD)."""

    start: date
    end: date
    wealth_start: float | None
    wealth_end: float | None
    external: float
    price_pnl: float | None
    income: float | None
    corporate_action_cash: float
    explicit_costs: float | None
    fx_translation: float
    residual: float | None
    status: str  # "reconciled" | "unreconciled" | "unavailable"
    unavailable_reason: str | None = None

    @property
    def net_pnl(self) -> float | None:
        if self.status != "reconciled":
            return None
        assert self.wealth_start is not None and self.wealth_end is not None
        return self.wealth_end - self.wealth_start - self.external

    @property
    def gross_pnl(self) -> float | None:
        net = self.net_pnl
        return None if net is None or self.explicit_costs is None else net + self.explicit_costs


def _unavailable(start: WealthObservation, end: WealthObservation, external: float, why: str) -> PeriodPnL:
    return PeriodPnL(
        start=start.when, end=end.when,
        wealth_start=start.wealth, wealth_end=end.wealth, external=external,
        price_pnl=None, income=None, corporate_action_cash=0.0, explicit_costs=None,
        fx_translation=0.0, residual=None, status="unavailable", unavailable_reason=why,
    )


def decompose_period(
    start: WealthObservation,
    end: WealthObservation,
    events: Iterable[AccountEvent],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> PeriodPnL:
    """Decompose ``(start.when, end.when]`` and gate it on the wealth identity.

    The cash source may not switch inside a period: a switch closes one period and
    opens the next, so here it makes the period unavailable rather than letting two
    cash series meet in one difference."""
    evs = [e for e in events if start.when < e.when <= end.when]
    external = sum(account_external_flow(e) for e in evs)
    for obs in (start, end):
        if obs.unavailable_reason is not None:
            return _unavailable(start, end, external, obs.unavailable_reason)
    if start.cash_source != end.cash_source:
        return _unavailable(
            start, end, external,
            f"cash source switches inside the period ({start.cash_source} -> {end.cash_source})",
        )
    assert start.cash is not None and end.cash is not None
    assert start.accrued_income is not None and end.accrued_income is not None
    assert start.financing_debit is not None and end.financing_debit is not None
    assert start.wealth is not None and end.wealth is not None

    def total(*kinds: EventKind) -> float:
        return sum(e.amount for e in evs if e.kind in kinds)

    buys, sells = total(EventKind.BUY), total(EventKind.SELL)
    commissions = sum(e.fees for e in evs if e.kind in (EventKind.BUY, EventKind.SELL))
    ca_cash = total(EventKind.CORPORATE_ACTION_CASH)
    # FX translation is the part of the holdings' USD move that is currency, not price.
    fx = total(EventKind.FX_TRANSLATION)
    price_pnl = (
        end.holdings_mv - start.holdings_mv - buys + sells
        - total(EventKind.TRANSFER_IN) + total(EventKind.TRANSFER_OUT) - fx
    )
    income = (
        total(EventKind.DIVIDEND, EventKind.INTEREST)
        + end.accrued_income - start.accrued_income
    )
    costs = (
        commissions + total(EventKind.FEE, EventKind.FINANCING)
        + end.financing_debit - start.financing_debit
    )
    explained = price_pnl + income + ca_cash - costs + fx
    residual = round(end.wealth - start.wealth - external - explained, 10)
    reconciled = abs(residual) <= tolerance
    return PeriodPnL(
        start=start.when, end=end.when,
        wealth_start=start.wealth, wealth_end=end.wealth, external=external,
        price_pnl=price_pnl, income=income, corporate_action_cash=ca_cash,
        explicit_costs=costs, fx_translation=fx, residual=residual,
        status="reconciled" if reconciled else UNRECONCILED,
        unavailable_reason=None if reconciled else (
            f"{UNRECONCILED}: wealth identity residual {residual:+.2f} exceeds "
            f"tolerance {tolerance:.2f}"
        ),
    )


@dataclass(frozen=True)
class Metric:
    """A performance figure that says what it is. ``value`` is ``None`` exactly when
    ``unavailable_reason`` is set."""

    name: str
    value: float | None
    basis: str
    units: str
    start: date
    end: date
    n: int
    uncertainty: float | None = None
    unavailable_reason: str | None = None
    contract: str = CONTRACT


@dataclass(frozen=True)
class AccountPerformance:
    periods: tuple[PeriodPnL, ...]
    metrics: dict[str, Metric]


def account_performance(
    observations: Sequence[WealthObservation],
    events: Iterable[AccountEvent],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> AccountPerformance:
    """Daily-linked account-basis performance over consecutive observations.

    Each consecutive pair of observations is one period, normally one day. External
    flows are at the START of the day they land on, so a period's capital at work is
    ``W_start + external``. Any period that is not reconciled makes every net and
    gross metric unavailable with that period's reason."""
    obs = sorted(observations, key=lambda o: o.when)
    evs = sorted(events, key=lambda e: e.when)
    if len(obs) < 2:
        raise ValueError("account_performance needs at least two observations")
    periods = tuple(decompose_period(a, b, evs, tolerance=tolerance) for a, b in zip(obs, obs[1:], strict=False))
    start, end, n = obs[0].when, obs[-1].when, len(periods)

    def metric(name: str, value: float | None, units: str, why: str | None) -> Metric:
        return Metric(name, None if why else value, BASIS_ACCOUNT, units, start, end, n,
                      unavailable_reason=why)

    broken = next((p for p in periods if p.status != "reconciled"), None)
    why = None if broken is None else (
        f"period {broken.start.isoformat()}..{broken.end.isoformat()}: {broken.unavailable_reason}"
    )
    net_growth = gross_growth = 1.0
    for p in periods:
        if why:
            break
        assert p.wealth_start is not None and p.wealth_end is not None
        at_work = p.wealth_start + p.external
        if at_work <= 0:
            why = f"non-positive capital at work on {p.end.isoformat()}"
            break
        assert p.explicit_costs is not None
        net_growth *= p.wealth_end / at_work
        gross_growth *= (p.wealth_end + p.explicit_costs) / at_work

    mwr: float | None = None
    mwr_why = why
    if not why:
        assert obs[0].wealth is not None and obs[-1].wealth is not None
        flows = [CashFlow(start, -obs[0].wealth)]
        flows += [CashFlow(e.when, -account_external_flow(e))
                  for e in evs if e.kind in EXTERNAL_KINDS and start < e.when <= end]
        flows.append(CashFlow(end, obs[-1].wealth))
        mwr = xirr(flows)
        if mwr is None:
            mwr_why = "XIRR did not converge"

    net_pnl = sum(p.net_pnl or 0.0 for p in periods)
    costs = sum(p.explicit_costs or 0.0 for p in periods)
    metrics = {
        "twr_net": metric("twr_net", net_growth - 1.0, "fraction", why),
        "twr_gross": metric("twr_gross", gross_growth - 1.0, "fraction", why),
        "mwr_net_annualized": metric("mwr_net_annualized", mwr, "fraction_per_year", mwr_why),
        "net_pnl": metric("net_pnl", net_pnl, "USD", why),
        "gross_pnl": metric("gross_pnl", net_pnl + costs, "USD", why),
        "explicit_costs": metric("explicit_costs", costs, "USD", why),
    }
    return AccountPerformance(periods=periods, metrics=metrics)
