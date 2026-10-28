"""Full-account performance engine, ``account_performance.v1`` (metron-ops#371, slices 1-3c).

The holdings-sleeve figures in ``api.services.performance`` value priced holdings only
and treat net purchases as the flow. That is right for the sleeve and wrong for an
account. This module is the *account* basis. It is pure, stdlib plus the published
quant core, and it changes no API. Slice 3 records its inputs (cash and cash source) on
each per-account snapshot and maps a stored row onto :class:`WealthObservation`
(``api.services.account_basis``), and slice 3b serves it, opt-in, as
``GET /portfolios/{id}/performance?basis=account``. Slice 3c adds the portfolio-level
rollup across accounts (:func:`combine_observations`).

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
member yet (metron-ops#378), so ``from_transaction`` never produces one. A transfer can
only come from a caller that knows it is one.

**Position gate (slice 2).** Price P&L is derived from the holdings move, so a position
that arrives with no classifying event (a transfer in kind the ledger cannot record)
would otherwise read as investment return. Every observation therefore carries its
positions, and a period whose quantity change its events do not explain is
``unreconciled``. Positions that are not observed make the period unavailable: a
transfer in kind cannot be ruled out, and an unchecked period never counts as clean.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from nousergon_lib.quant.returns import CashFlow, xirr

from portfolio_analytics.domain.ledger import Transaction, TxnType

CONTRACT = "account_performance.v1"
BASIS_ACCOUNT = "account"
DEFAULT_TOLERANCE = 0.01  # one cent
DEFAULT_QUANTITY_TOLERANCE = 1e-6  # shares; fractional-share brokers report to 1e-4 or finer
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
    is the commission. For a transfer it is the transfer-date market value.

    ``symbol`` and ``quantity`` are what the position gate reads: shares for
    BUY/SELL/TRANSFER_IN/TRANSFER_OUT, the new:old ratio for SPLIT. Other kinds move no
    position."""

    when: date
    kind: EventKind
    amount: float = 0.0
    fees: float = 0.0
    symbol: str = ""
    quantity: float = 0.0


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
        return AccountEvent(txn.when, kind, amount=txn.quantity * txn.price, fees=txn.fees,
                            symbol=txn.ticker, quantity=txn.quantity)
    if kind is EventKind.SPLIT:
        return AccountEvent(txn.when, kind, symbol=txn.ticker, quantity=txn.quantity)
    return AccountEvent(txn.when, kind, amount=txn.amount, fees=txn.fees, symbol=txn.ticker)


def account_external_flow(event: AccountEvent) -> float:
    """External capital on the ACCOUNT basis, from the account's side: ``> 0`` in,
    ``< 0`` out, ``0`` for every internal event. A reinvested dividend is a BUY and
    therefore never external here, unlike on the holdings sleeve."""
    if event.kind in (EventKind.DEPOSIT, EventKind.TRANSFER_IN):
        return event.amount
    if event.kind in (EventKind.WITHDRAWAL, EventKind.TRANSFER_OUT):
        return -event.amount
    return 0.0


_POSITION_SIGN: dict[EventKind, float] = {
    EventKind.BUY: 1.0,
    EventKind.TRANSFER_IN: 1.0,
    EventKind.SELL: -1.0,
    EventKind.TRANSFER_OUT: -1.0,
}


def unexplained_position_changes(
    start: Mapping[str, float],
    end: Mapping[str, float],
    events: Iterable[AccountEvent],
    *,
    tolerance: float = DEFAULT_QUANTITY_TOLERANCE,
) -> dict[str, float]:
    """The account-basis flow classifier's position half: for each symbol, the quantity
    change between two position observations that ``events`` do not explain.

    Events are applied in the order given, which the callers keep as date order: a SPLIT
    scales the running quantity by its ratio, and BUY/TRANSFER_IN add while SELL/
    TRANSFER_OUT subtract. A symbol absent from a mapping holds zero. Only differences
    beyond ``tolerance`` are returned, signed as ``observed − explained``."""
    expected: dict[str, float] = {s: float(q) for s, q in start.items()}
    for e in events:
        if e.kind is EventKind.SPLIT:
            if e.symbol in expected:
                expected[e.symbol] *= e.quantity
        elif e.kind in _POSITION_SIGN:
            expected[e.symbol] = expected.get(e.symbol, 0.0) + _POSITION_SIGN[e.kind] * e.quantity
    out: dict[str, float] = {}
    for symbol in sorted(set(expected) | set(end)):
        gap = float(end.get(symbol, 0.0)) - expected.get(symbol, 0.0)
        if abs(gap) > tolerance:
            out[symbol] = gap
    return out


@dataclass(frozen=True)
class WealthObservation:
    """Account components observed at one date's close. ``None`` means not observed.

    ``positions`` is quantity by symbol at the same close. It is not a wealth component,
    so it does not make ``wealth`` unavailable; :func:`decompose_period` gates on it."""

    when: date
    holdings_mv: float
    cash: float | None
    cash_source: str | None = None  # "custodian" | "ledger"
    accrued_income: float | None = 0.0
    financing_debit: float | None = 0.0
    positions: Mapping[str, float] | None = None

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


def combine_observations(observations: Sequence[WealthObservation]) -> WealthObservation:
    """Several accounts' observations at ONE close as one observation, for the
    portfolio-level rollup (metron-ops#371, slice 3c).

    Every monetary component is summed, and a component that any account did not observe
    leaves the combined one unobserved: an account's missing cash is never read as zero,
    so the whole-portfolio wealth is unavailable rather than understated. Positions are
    summed by symbol, so a transfer in kind between two accounts in the rollup leaves
    the combined positions unchanged and is not a break at this level.

    ``cash_source`` is the shared source when every account has the same one. Otherwise
    it is the per-account sources in the order given, so the engine's same-source rule
    still closes a period when any single account switches source. Callers keep the
    order stable across dates."""
    if not observations:
        raise ValueError("combine_observations needs at least one observation")
    dates = {o.when for o in observations}
    if len(dates) != 1:
        raise ValueError(f"observations span {len(dates)} dates; a rollup combines one close")

    def summed(values: Iterable[float | None]) -> float | None:
        vals = list(values)
        return None if any(v is None for v in vals) else float(sum(v for v in vals if v is not None))

    positions: dict[str, float] | None = {}
    for o in observations:
        if o.positions is None:
            positions = None
            break
        for symbol, qty in o.positions.items():
            positions[symbol] = positions.get(symbol, 0.0) + float(qty)
    sources = [o.cash_source or "" for o in observations]
    cash_source = sources[0] if len(set(sources)) == 1 else "mixed:" + ",".join(sources)
    cash = summed(o.cash for o in observations)
    return WealthObservation(
        when=observations[0].when,
        holdings_mv=float(sum(o.holdings_mv for o in observations)),
        cash=cash,
        cash_source=cash_source if cash is not None else None,
        accrued_income=summed(o.accrued_income for o in observations),
        financing_debit=summed(o.financing_debit for o in observations),
        positions=positions,
    )


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
    quantity_tolerance: float = DEFAULT_QUANTITY_TOLERANCE,
) -> PeriodPnL:
    """Decompose ``(start.when, end.when]`` and gate it on the wealth identity and on
    positions.

    The cash source may not switch inside a period: a switch closes one period and
    opens the next, so here it makes the period unavailable rather than letting two
    cash series meet in one difference. A quantity change the period's events do not
    explain makes it unreconciled even when the cash identity closes, because the
    identity derives price P&L from the holdings move and would absorb it."""
    evs = [e for e in events if start.when < e.when <= end.when]
    external = sum(account_external_flow(e) for e in evs)
    for obs in (start, end):
        if obs.unavailable_reason is not None:
            return _unavailable(start, end, external, obs.unavailable_reason)
        if obs.positions is None:
            return _unavailable(
                start, end, external,
                f"positions not observed on {obs.when.isoformat()}: "
                "a transfer in kind cannot be ruled out",
            )
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
    assert start.positions is not None and end.positions is not None
    jumps = unexplained_position_changes(
        start.positions, end.positions, evs, tolerance=quantity_tolerance
    )
    reconciled = abs(residual) <= tolerance and not jumps
    if jumps:
        listed = ", ".join(f"{s} {q:+.6g}" for s, q in jumps.items())
        why = (
            f"{UNRECONCILED}: position change with no classifying event ({listed}); "
            "a transfer in kind has no ledger member yet (metron-ops#378)"
        )
    elif not reconciled:
        why = (
            f"{UNRECONCILED}: wealth identity residual {residual:+.2f} exceeds "
            f"tolerance {tolerance:.2f}"
        )
    else:
        why = None
    return PeriodPnL(
        start=start.when, end=end.when,
        wealth_start=start.wealth, wealth_end=end.wealth, external=external,
        price_pnl=price_pnl, income=income, corporate_action_cash=ca_cash,
        explicit_costs=costs, fx_translation=fx, residual=residual,
        status="reconciled" if reconciled else UNRECONCILED,
        unavailable_reason=why,
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
    quantity_tolerance: float = DEFAULT_QUANTITY_TOLERANCE,
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
    periods = tuple(
        decompose_period(a, b, evs, tolerance=tolerance, quantity_tolerance=quantity_tolerance)
        for a, b in zip(obs, obs[1:], strict=False)
    )
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
