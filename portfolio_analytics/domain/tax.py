"""Tax lens over unrealized positions — holding period, est. tax, harvestable loss.

Pure stdlib, data-source-agnostic. Classifies each position's gain as short- vs
long-term by holding period, estimates the tax due if it were sold today, and
flags loss positions as tax-loss-harvesting candidates. All **descriptive** — an
estimate and an information surface, never advice, and the holding period is an
*estimate* (no per-lot acquisition data; see the loader caveats).

Also measures a user-authored hypothetical sale of one holding (``hypothetical_sale``):
lot relief (FIFO or user-chosen specific lots), the short-/long-term split, and a
flat-rate tax estimate. The user supplies the action; this module supplies only the
measurement.

Conventions: gains/losses and values are USD. Tax is charged on gains only
(losses incur none); an Unknown holding period is treated as short-term — the
conservative (higher-rate) assumption — and labeled as such.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from portfolio_analytics.domain.ledger import Lot, relieve_lots

LONG_TERM_DAYS = 365  # > 1 year held ⇒ long-term capital gains (US convention)

SHORT_TERM = "Short-term"
LONG_TERM = "Long-term"
UNKNOWN = "Unknown"


def holding_period_days(acq_date: str | None, asof: date) -> int | None:
    """Days from an ISO ``acq_date`` to ``asof``, or None if unparseable/missing."""
    if not acq_date:
        return None
    try:
        acq = date.fromisoformat(str(acq_date)[:10])
    except ValueError:
        return None
    return (asof - acq).days


def classify_term(days: int | None, *, long_term_days: int = LONG_TERM_DAYS) -> str:
    """``Short-term`` / ``Long-term`` / ``Unknown`` from a holding-period length."""
    if days is None:
        return UNKNOWN
    return LONG_TERM if days > long_term_days else SHORT_TERM


def tax_on_gain(gain: float, term: str, *, short_term_rate: float, long_term_rate: float) -> float:
    """Estimated tax on a position's gain (0 for a loss).

    Long-term gains use ``long_term_rate``; short-term **and Unknown** use
    ``short_term_rate`` (the conservative assumption when the term can't be dated).
    """
    if gain <= 0:
        return 0.0
    rate = long_term_rate if term == LONG_TERM else short_term_rate
    return gain * rate


def harvestable_loss(unrealized_gain: float) -> float:
    """The loss available to harvest (positive) for a below-cost position, else 0."""
    return -unrealized_gain if unrealized_gain < 0 else 0.0


# ── "If sold" — tax math on a hypothetical sale the user authors (metron-ops#208) ──
#
# A calculator, not a recommendation: the caller supplies the holding, the quantity, the
# price and the lot-selection method; this module only measures what that hypothetical
# would realize. Nothing here chooses a position, a quantity, or a lot.


class LotMethod(StrEnum):
    """How the hypothetical sale relieves lots."""

    FIFO = "fifo"  # oldest lot first — the IRS default when no lot is identified
    SPECIFIC = "specific"  # the user names the lots and the quantity from each


@dataclass(frozen=True)
class TaxRates:
    """Flat rates applied to the hypothetical's net gain — user inputs, never computed.

    ``short_term``/``long_term`` are federal rates on net short-/long-term gain;
    ``state`` is an optional flat rate on the total net gain. Each is a fraction in
    ``[0, 1]`` (0.24 = 24%).
    """

    short_term: float
    long_term: float
    state: float = 0.0

    def __post_init__(self) -> None:
        for name in ("short_term", "long_term", "state"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} rate must be between 0 and 1, got {value}")


@dataclass(frozen=True)
class LotPick:
    """One user-chosen lot for a specific-lot hypothetical: the lot's index in the
    holding's lot list, and how many of its shares to include."""

    lot_index: int
    quantity: float


@dataclass(frozen=True)
class SoldLot:
    """One lot (or part of one) relieved by the hypothetical sale."""

    lot_index: int
    open_date: date
    quantity: float
    cost_per_share: float
    proceeds: float
    cost_basis: float
    gain: float
    holding_days: int
    term: str


@dataclass(frozen=True)
class HypotheticalSale:
    """The measured outcome of one hypothetical sale.

    ``gain_st``/``gain_lt`` are the raw per-term sums. ``taxable_st``/``taxable_lt`` are
    what remains after netting one term's loss against the other term's gain (the US
    netting order), and the estimates apply the rates to those. The estimate covers this
    hypothetical ALONE: it ignores other gains/losses realized in the year, carryforwards,
    brackets, NIIT and AMT, and a net loss shows zero tax (its deduction value is not
    estimated).
    """

    method: LotMethod
    quantity: float
    price: float
    proceeds: float
    cost_basis: float
    gain_st: float
    gain_lt: float
    gain_total: float
    taxable_st: float
    taxable_lt: float
    est_tax_st: float
    est_tax_lt: float
    est_tax_state: float
    est_tax_total: float
    lots: tuple[SoldLot, ...]


@dataclass(frozen=True)
class SaleDelta:
    """``selected − baseline`` for the headline figures (e.g. specific-lot vs FIFO)."""

    cost_basis: float
    gain_st: float
    gain_lt: float
    gain_total: float
    est_tax_total: float


_EPS = 1e-9


def net_capital_gains(gain_st: float, gain_lt: float) -> tuple[float, float]:
    """Net a loss in one term against a gain in the other; return the taxable
    ``(short_term, long_term)`` amounts, each ``>= 0``."""
    if gain_st >= 0 and gain_lt >= 0:
        return gain_st, gain_lt
    if gain_st < 0 and gain_lt < 0:
        return 0.0, 0.0
    if gain_st < 0:  # short-term loss absorbs long-term gain
        return 0.0, max(gain_lt + gain_st, 0.0)
    return max(gain_st + gain_lt, 0.0), 0.0  # long-term loss absorbs short-term gain


def hypothetical_sale(
    lots: Sequence[Lot],
    *,
    quantity: float,
    price: float,
    asof: date,
    rates: TaxRates,
    method: LotMethod = LotMethod.FIFO,
    picks: Sequence[LotPick] | None = None,
) -> HypotheticalSale:
    """Measure selling ``quantity`` shares of one holding at ``price`` on ``asof``.

    ``lots`` are the holding's open lots; a lot's index in this sequence is its identity
    for ``picks``. FIFO relieves by open date (ties keep sequence order). SPECIFIC
    relieves exactly the ``picks`` given, in the order given, and their quantities must
    sum to ``quantity``. Nothing is mutated — relief runs on copies through the ledger's
    own ``relieve_lots``, and the term comes from ``classify_term`` on the lot's holding
    period, so this shares the ledger's and the tax lens's rules rather than restating
    them.

    Raises ``ValueError`` on a non-positive quantity, a negative price, a quantity above
    the shares held, or an invalid pick.
    """
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")
    if price < 0:
        raise ValueError("price must not be negative")
    held = sum(lot.quantity for lot in lots)
    if quantity > held + _EPS:
        raise ValueError(f"quantity {quantity:g} exceeds the {held:g} shares held")

    if method is LotMethod.FIFO:
        order = sorted(range(len(lots)), key=lambda i: lots[i].open_date)
        chosen = [(i, lots[i].quantity) for i in order if lots[i].quantity > _EPS]
    else:
        chosen = _validated_picks(lots, quantity, picks)

    copies = [Lot(lots[i].ticker, lots[i].open_date, q, lots[i].cost_per_share) for i, q in chosen]
    realized = relieve_lots(copies, quantity, close_date=asof, proceeds_per_share=price)

    sold: list[SoldLot] = []
    for (index, _), gain in zip(chosen, realized, strict=False):
        sold.append(
            SoldLot(
                lot_index=index,
                open_date=gain.open_date,
                quantity=gain.quantity,
                cost_per_share=lots[index].cost_per_share,
                proceeds=gain.proceeds,
                cost_basis=gain.cost_basis,
                gain=gain.gain,
                holding_days=gain.holding_days,
                term=classify_term(gain.holding_days),
            )
        )

    gain_st = sum(s.gain for s in sold if s.term != LONG_TERM)
    gain_lt = sum(s.gain for s in sold if s.term == LONG_TERM)
    taxable_st, taxable_lt = net_capital_gains(gain_st, gain_lt)
    tax_st = tax_on_gain(taxable_st, SHORT_TERM, short_term_rate=rates.short_term, long_term_rate=rates.long_term)
    tax_lt = tax_on_gain(taxable_lt, LONG_TERM, short_term_rate=rates.short_term, long_term_rate=rates.long_term)
    tax_state = (taxable_st + taxable_lt) * rates.state
    proceeds = sum(s.proceeds for s in sold)
    cost_basis = sum(s.cost_basis for s in sold)
    return HypotheticalSale(
        method=method,
        quantity=quantity,
        price=price,
        proceeds=proceeds,
        cost_basis=cost_basis,
        gain_st=gain_st,
        gain_lt=gain_lt,
        gain_total=gain_st + gain_lt,
        taxable_st=taxable_st,
        taxable_lt=taxable_lt,
        est_tax_st=tax_st,
        est_tax_lt=tax_lt,
        est_tax_state=tax_state,
        est_tax_total=tax_st + tax_lt + tax_state,
        lots=tuple(sold),
    )


def _validated_picks(
    lots: Sequence[Lot], quantity: float, picks: Sequence[LotPick] | None
) -> list[tuple[int, float]]:
    if not picks:
        raise ValueError("specific-lot selection needs at least one lot")
    seen: set[int] = set()
    chosen: list[tuple[int, float]] = []
    for pick in picks:
        if not 0 <= pick.lot_index < len(lots):
            raise ValueError(f"lot {pick.lot_index} does not exist")
        if pick.lot_index in seen:
            raise ValueError(f"lot {pick.lot_index} is selected more than once")
        seen.add(pick.lot_index)
        if pick.quantity <= 0:
            raise ValueError(f"lot {pick.lot_index}: quantity must be greater than zero")
        available = lots[pick.lot_index].quantity
        if pick.quantity > available + _EPS:
            raise ValueError(f"lot {pick.lot_index}: quantity {pick.quantity:g} exceeds its {available:g} shares")
        chosen.append((pick.lot_index, pick.quantity))
    picked = sum(q for _, q in chosen)
    if abs(picked - quantity) > 1e-6:
        raise ValueError(f"selected lots total {picked:g} shares, not the {quantity:g} in the hypothetical")
    return chosen


def sale_delta(selected: HypotheticalSale, baseline: HypotheticalSale) -> SaleDelta:
    """Difference ``selected − baseline`` (e.g. a specific-lot hypothetical vs FIFO)."""
    return SaleDelta(
        cost_basis=selected.cost_basis - baseline.cost_basis,
        gain_st=selected.gain_st - baseline.gain_st,
        gain_lt=selected.gain_lt - baseline.gain_lt,
        gain_total=selected.gain_total - baseline.gain_total,
        est_tax_total=selected.est_tax_total - baseline.est_tax_total,
    )
