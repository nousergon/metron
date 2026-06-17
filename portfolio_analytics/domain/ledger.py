"""Transaction & tax-lot ledger — the descriptive data substrate (F1).

Processes a chronological list of transactions into **tax lots**, **realized
gains** (short/long-term), open positions, and the **external cash flows** the
return engine needs. Pure stdlib, data-source-agnostic (takes a transaction
list, not a broker client) so it's unit-testable in isolation and reusable
unchanged across front ends.

This is accounting, not advice — it records what happened. It feeds:
  - `analytics.returns` (external cash flows → XIRR; valuations → TWR),
  - the v0 tax-*reporting* surface (realized/unrealized, holding period),
without ever prescribing a trade.

Lot relief defaults to **FIFO** (the IRS default when no specific lot is
identified). Fees fold into cost basis on buys and reduce proceeds on sells.
Holding period > 365 days ⇒ long-term (IRS "more than one year").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from alpha_engine_lib.quant.returns import CashFlow

_LONG_TERM_DAYS = 365


class TxnType(StrEnum):
    """Transaction kinds. DEPOSIT/WITHDRAWAL are the only *external* cash flows;
    BUY/SELL/DIVIDEND move cash within the portfolio (internal)."""

    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"  # cash-in like DIVIDEND; canonical layer needs a distinct member
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    FEE = "FEE"
    SPLIT = "SPLIT"  # quantity = new:old ratio (2.0 = 2-for-1)


@dataclass(frozen=True)
class Transaction:
    """A dated portfolio event. Cash amounts are positive magnitudes; the type
    determines direction. ``quantity``/``price`` apply to BUY/SELL (shares,
    per-share); ``quantity`` is the ratio for SPLIT; ``amount`` is the cash
    magnitude for DEPOSIT/WITHDRAWAL/DIVIDEND/FEE."""

    when: date
    type: TxnType
    ticker: str = ""
    quantity: float = 0.0
    price: float = 0.0
    amount: float = 0.0
    fees: float = 0.0
    currency: str = "USD"


@dataclass
class Lot:
    """An open tax lot — remaining shares acquired on one date at one basis."""

    ticker: str
    open_date: date
    quantity: float
    cost_per_share: float

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.cost_per_share


@dataclass(frozen=True)
class RealizedGain:
    """A closed lot (or partial), with proceeds, cost basis, and holding period."""

    ticker: str
    open_date: date
    close_date: date
    quantity: float
    proceeds: float
    cost_basis: float

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost_basis

    @property
    def holding_days(self) -> int:
        return (self.close_date - self.open_date).days

    @property
    def long_term(self) -> bool:
        return self.holding_days > _LONG_TERM_DAYS


@dataclass
class Ledger:
    """Result of processing transactions: open lots + realized gains + cash."""

    open_lots: dict[str, list[Lot]] = field(default_factory=dict)
    realized: list[RealizedGain] = field(default_factory=list)
    cash: float = 0.0

    def position(self, ticker: str) -> tuple[float, float]:
        """Return ``(total_shares, average_cost_per_share)`` for an open position.

        ``(0.0, 0.0)`` if no open lots. Average cost is share-weighted.
        """
        lots = self.open_lots.get(ticker, [])
        shares = sum(lot.quantity for lot in lots)
        if shares <= 0:
            return 0.0, 0.0
        basis = sum(lot.cost_basis for lot in lots)
        return shares, basis / shares

    def unrealized(self, prices: dict[str, float]) -> dict[str, float]:
        """Per-ticker unrealized gain at ``prices`` (market value − cost basis).

        Tickers absent from ``prices`` are skipped (can't value them).
        """
        out: dict[str, float] = {}
        for ticker, lots in self.open_lots.items():
            if ticker not in prices:
                continue
            shares = sum(lot.quantity for lot in lots)
            basis = sum(lot.cost_basis for lot in lots)
            out[ticker] = shares * prices[ticker] - basis
        return out

    def realized_totals(self) -> tuple[float, float]:
        """Return ``(short_term_gain, long_term_gain)`` summed over realized lots."""
        st = sum(r.gain for r in self.realized if not r.long_term)
        lt = sum(r.gain for r in self.realized if r.long_term)
        return st, lt


def build_ledger(transactions: list[Transaction]) -> Ledger:
    """Process transactions chronologically into a `Ledger` (FIFO lot relief).

    Raises ``ValueError`` on a SELL exceeding shares held (fail loud — a sell of
    unheld shares is a data-integrity error, never silently clamped).
    """
    ledger = Ledger()
    for txn in sorted(transactions, key=lambda t: t.when):
        _apply(ledger, txn)
    return ledger


def _apply(ledger: Ledger, txn: Transaction) -> None:
    if txn.type is TxnType.DEPOSIT:
        ledger.cash += txn.amount
    elif txn.type is TxnType.WITHDRAWAL:
        ledger.cash -= txn.amount
    elif txn.type is TxnType.DIVIDEND:
        ledger.cash += txn.amount
    elif txn.type is TxnType.INTEREST:
        ledger.cash += txn.amount
    elif txn.type is TxnType.FEE:
        ledger.cash -= txn.amount
    elif txn.type is TxnType.BUY:
        _buy(ledger, txn)
    elif txn.type is TxnType.SELL:
        _sell(ledger, txn)
    elif txn.type is TxnType.SPLIT:
        _split(ledger, txn)


def _buy(ledger: Ledger, txn: Transaction) -> None:
    if txn.quantity <= 0:
        return
    # Per-share cost. A broker's money-market / cash-sweep BUY (FDRXX, SPAXX, a 401(k)
    # CIT) frequently reports price=0 — or omits it — while ``amount`` carries the true
    # cash invested. Using price then gives a $0 cost basis for a fund actually worth its
    # full NAV (metron-ops#61). Fall back to amount/quantity when price is missing; price
    # stays authoritative whenever it's reported. ``amount`` is a positive magnitude that
    # already includes fees, so don't double-count them in that branch.
    if txn.price > 0:
        cost_per_share = txn.price + txn.fees / txn.quantity  # fees → cost basis
        cash_out = txn.quantity * txn.price + txn.fees
    elif txn.amount > 0:
        cost_per_share = txn.amount / txn.quantity
        cash_out = txn.amount
    else:
        cost_per_share = txn.fees / txn.quantity  # degenerate: neither price nor amount
        cash_out = txn.fees
    ledger.open_lots.setdefault(txn.ticker, []).append(
        Lot(ticker=txn.ticker, open_date=txn.when, quantity=txn.quantity, cost_per_share=cost_per_share)
    )
    ledger.cash -= cash_out


def _sell(ledger: Ledger, txn: Transaction) -> None:
    if txn.quantity <= 0:
        return
    lots = ledger.open_lots.get(txn.ticker, [])
    available = sum(lot.quantity for lot in lots)
    if txn.quantity > available + 1e-9:
        raise ValueError(f"SELL of {txn.quantity} {txn.ticker} on {txn.when} exceeds {available} shares held")
    # Proceeds net of fees, allocated across closed lots pro-rata by share.
    net_proceeds_per_share = txn.price - txn.fees / txn.quantity
    remaining = txn.quantity
    while remaining > 1e-9 and lots:
        lot = lots[0]
        closed = min(lot.quantity, remaining)
        ledger.realized.append(
            RealizedGain(
                ticker=txn.ticker,
                open_date=lot.open_date,
                close_date=txn.when,
                quantity=closed,
                proceeds=closed * net_proceeds_per_share,
                cost_basis=closed * lot.cost_per_share,
            )
        )
        lot.quantity -= closed
        remaining -= closed
        if lot.quantity <= 1e-9:
            lots.pop(0)
    ledger.cash += txn.quantity * txn.price - txn.fees


def _split(ledger: Ledger, txn: Transaction) -> None:
    """Apply a stock split: ratio = new:old (2.0 = 2-for-1). Shares ×ratio, basis
    ÷ratio per share, so total cost basis is preserved. No-op if no open lots."""
    if txn.quantity <= 0:
        return
    for lot in ledger.open_lots.get(txn.ticker, []):
        lot.quantity *= txn.quantity
        lot.cost_per_share /= txn.quantity


def external_cash_flows(transactions: list[Transaction]) -> list[CashFlow]:
    """Extract investor-perspective external cash flows for XIRR.

    Only DEPOSIT/WITHDRAWAL are external (money into/out of the portfolio). Sign
    convention matches `analytics.returns.xirr`: a DEPOSIT (money you put in) is
    **negative**; a WITHDRAWAL (money you took out) is **positive**. The caller
    appends the terminal portfolio value as a final positive flow.
    """
    flows: list[CashFlow] = []
    for txn in sorted(transactions, key=lambda t: t.when):
        if txn.type is TxnType.DEPOSIT:
            flows.append(CashFlow(txn.when, -txn.amount))
        elif txn.type is TxnType.WITHDRAWAL:
            flows.append(CashFlow(txn.when, txn.amount))
    return flows
