"""SnapTrade activities → tax-lot tranches.

The holdings endpoint exposes only one aggregate ``average_purchase_price`` per
position. To recover the individual **tranches** (the lots that make up a
position), we replay the per-account *activity* history through the FIFO ledger
in ``analytics.ledger``.

Two-step, both pure and unit-testable:
  1. ``activities_to_transactions`` — map raw SnapTrade activity dicts to the
     ledger's ``Transaction`` model.
  2. ``reconstruct_tranches`` — replay per ticker, reconcile the reconstructed
     share count against the broker's authoritative holdings, and surface any
     **history-depth gap** honestly (lots opened before SnapTrade's available
     activity window can't be reconstructed) rather than silently showing a
     partial position as if complete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from portfolio_analytics.domain.ledger import Lot, Transaction, TxnType, build_ledger

logger = logging.getLogger(__name__)

# Shares can be fractional; tolerate float dust when reconciling reconstructed
# vs broker-reported share counts.
_SHARE_TOL = 1e-4

# SnapTrade activity `type` (case-insensitive) → ledger TxnType. Types that only
# move cash (dividends, fees, transfers) are mapped so the ledger stays coherent,
# but the tranche view reads only BUY/SELL-derived lots. Unmapped types skip.
_TYPE_MAP = {
    "BUY": TxnType.BUY,
    "SELL": TxnType.SELL,
    "DIVIDEND": TxnType.DIVIDEND,
    "CONTRIBUTION": TxnType.DEPOSIT,
    "DEPOSIT": TxnType.DEPOSIT,
    "WITHDRAWAL": TxnType.WITHDRAWAL,
    "FEE": TxnType.FEE,
}


def _f(value, default: float = 0.0) -> float:
    """Coerce a possibly-None/str numeric to float, falling back to ``default``."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_ticker(activity: dict) -> str:
    """Pull the equity ticker from a SnapTrade activity's nested symbol object.

    Activity symbols carry the ticker at ``symbol.symbol`` (a string); some
    payloads nest one level deeper (``symbol.symbol.symbol``). Returns "" when
    there's no equity symbol (cash/transfer activities).
    """
    sym = activity.get("symbol")
    if not isinstance(sym, dict):
        return ""
    inner = sym.get("symbol")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, dict):
        return inner.get("symbol", "") or ""
    return ""


def _extract_currency(activity: dict) -> str:
    """Native currency code from the symbol, then the activity, else USD."""
    sym = activity.get("symbol")
    if isinstance(sym, dict) and isinstance(sym.get("currency"), dict):
        code = sym["currency"].get("code")
        if code:
            return code
    cur = activity.get("currency")
    if isinstance(cur, dict) and cur.get("code"):
        return cur["code"]
    return "USD"


def _parse_date(value) -> date | None:
    """Parse a SnapTrade trade_date (ISO string or date/datetime) to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _to_transaction(a: dict) -> Transaction | None:
    """Map one SnapTrade activity dict to a ``Transaction``, or None if unusable.

    Returns None for unknown activity types or trades with no parseable date —
    we skip rather than guess, since a wrong lot is worse than a missing one.
    """
    txn_type = _TYPE_MAP.get(str(a.get("type", "")).upper().strip())
    if txn_type is None:
        return None
    when = _parse_date(a.get("trade_date") or a.get("settlement_date"))
    if when is None:
        return None
    return Transaction(
        when=when,
        type=txn_type,
        ticker=_extract_ticker(a),
        quantity=abs(_f(a.get("units"))),
        price=_f(a.get("price")),
        amount=abs(_f(a.get("amount"))),
        fees=abs(_f(a.get("fee"))),
        currency=_extract_currency(a),
    )


def activities_to_transactions(activities: list[dict]) -> list[Transaction]:
    """Map raw SnapTrade activity dicts to ledger ``Transaction`` objects.

    Unmapped types and undated trades are dropped. BUY/SELL carry
    units/price/fees; cash types carry ``amount``.
    """
    return [t for a in activities if (t := _to_transaction(a)) is not None]


def activities_to_transactions_aligned(activities: list[dict]) -> list[Transaction | None]:
    """Like ``activities_to_transactions`` but 1:1 with ``activities``.

    Keeps a None placeholder for each skipped activity so callers can zip the
    result back against the originals (to recover each transaction's account).
    """
    return [_to_transaction(a) for a in activities]


def group_transactions_by_account_ticker(activities: list[dict]) -> dict[tuple[str, str], list[Transaction]]:
    """Group mappable security transactions by ``(account_number, ticker)``.

    FIFO lot relief is **per account** — a sell in one account can't close a lot
    in another — so ``(account, ticker)`` is the correctness boundary for both
    tranche reconstruction and realized-gain reporting. This is the shared
    grouping both consume. Activities lacking an ``account_number`` (e.g. in
    tests) fall back to the empty-string account, i.e. ticker-only grouping.
    """
    by_group: dict[tuple[str, str], list[Transaction]] = {}
    for a, t in zip(activities, activities_to_transactions_aligned(activities), strict=True):
        if t is None or not t.ticker:
            continue
        by_group.setdefault((a.get("account_number", ""), t.ticker), []).append(t)
    return by_group


@dataclass
class TrancheSet:
    """Reconstructed open lots for one ticker, with a reconciliation verdict.

    ``lots`` are the FIFO-reconstructed open tranches (native currency, like the
    holding's ``avg_cost``). ``residual_shares`` > tolerance means the broker
    holds more shares than the available activity history accounts for — those
    pre-history shares are surfaced as a clearly-labeled synthetic lot priced at
    the holding's aggregate ``avg_cost`` so the totals still tie out.
    """

    ticker: str
    currency: str = "USD"
    lots: list[Lot] = field(default_factory=list)
    held_shares: float = 0.0
    reconstructed_shares: float = 0.0
    residual_shares: float = 0.0
    residual_cost_per_share: float = 0.0
    realized_st: float = 0.0
    realized_lt: float = 0.0
    complete: bool = True
    note: str = ""

    @property
    def has_synthetic_residual(self) -> bool:
        """True when a synthetic pre-history lot is needed to reconcile shares."""
        return self.residual_shares > _SHARE_TOL


def reconstruct_tranches(activities: list[dict], holdings: list[dict]) -> dict[str, TrancheSet]:
    """Replay activities into per-ticker open lots, reconciled to ``holdings``.

    ``holdings`` is a list of ``{ticker, shares, avg_cost, currency}`` dicts (the
    broker's authoritative, possibly cross-account-aggregated positions).

    FIFO lot relief is **per account** — a sell in one account can't close a lot
    in another — so transactions are grouped by ``(account_number, ticker)`` and
    each group replayed through its own ``build_ledger``. The resulting open lots
    are then merged per ticker for display (each lot keeps its own acquisition
    date/cost, so the union is the full tranche set). Isolating per group means
    one account/ticker's incomplete history — a SELL exceeding the BUYs we can
    see, which ``build_ledger`` raises on by design — is caught and flagged for
    that position alone, never crashing the whole view. Activities lacking an
    ``account_number`` (e.g. in tests) fall back to ticker-only grouping.
    """
    by_group = group_transactions_by_account_ticker(activities)

    # ticker → list of its (account, txns) groups
    groups_by_ticker: dict[str, list[list[Transaction]]] = {}
    for (_acct, ticker), group in by_group.items():
        groups_by_ticker.setdefault(ticker, []).append(group)

    out: dict[str, TrancheSet] = {}
    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker:
            continue
        held = _f(h.get("shares"))
        ts = TrancheSet(
            ticker=ticker,
            currency=h.get("currency", "USD") or "USD",
            held_shares=held,
            residual_cost_per_share=_f(h.get("avg_cost")),
        )
        groups = groups_by_ticker.get(ticker, [])
        if not groups:
            # No activity history at all → the entire position is pre-history.
            ts.residual_shares = held
            ts.complete = False
            ts.note = "No activity history — cost basis is the broker's aggregate average."
            out[ticker] = ts
            continue

        errored = False
        for group in groups:
            try:
                ledger = build_ledger(group)
            except ValueError as e:
                # SELL exceeding reconstructable BUYs → history starts mid-position.
                errored = True
                logger.debug("Incomplete history for %s: %s", ticker, e)
                continue
            ts.lots.extend(ledger.open_lots.get(ticker, []))
            st, lt = ledger.realized_totals()
            ts.realized_st += st
            ts.realized_lt += lt

        ts.lots.sort(key=lambda lot: lot.open_date)
        ts.reconstructed_shares = sum(lot.quantity for lot in ts.lots)
        ts.residual_shares = held - ts.reconstructed_shares
        errored_suffix = " (some history could not be replayed)" if errored else ""
        if errored:
            ts.complete = False
            ts.note = "Some activity history is incomplete — lots below are partial."
        if ts.residual_shares > _SHARE_TOL:
            ts.complete = False
            ts.note = (
                f"{ts.residual_shares:.4g} of {held:.4g} shares predate available "
                f"history — shown as a synthetic lot at the broker's average cost.{errored_suffix}"
            )
        elif ts.residual_shares < -_SHARE_TOL and not errored:
            # Reconstructed MORE than held (a transfer-out/return-of-capital the
            # history didn't capture). Flag, don't fabricate.
            ts.complete = False
            ts.note = (
                f"Reconstructed {ts.reconstructed_shares:.4g} shares but broker holds "
                f"{held:.4g} — history may be missing a disposal."
            )
        out[ticker] = ts
    return out
