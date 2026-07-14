"""Manual position entry — the third free-tier ingestion path (metron-ops#187).

A user with no brokerage connection and no exported CSV/OFX (e.g. holding 2-3
positions they'd rather type than hand-build a file for) can still get into
Metron: one ticker + quantity + cost basis (+ optional trade date) synthesizes a
single-row ``ConnectorSnapshot`` — the *same shape* the CSV importer produces
for a BUY row — so it lands through the identical persistence bridge and read
path (``api.services.persistence.persist_snapshot``). No parallel ingestion
path, no second symbol-validation path: this module does exactly what
``csv_import``'s per-row BUY handling does, just for one row supplied as
structured fields instead of parsed off a CSV line.

Positions are, as with CSV/OFX, ledger-derived at read time from the synthetic
BUY transaction — never written to the ``positions`` table directly (that table
is reserved for snapshot sources; see ``persistence._replace_positions``).

The synthesized activity carries ``amount = cost_basis`` (the user's stated
TOTAL, not per-share) and ``price = 0`` so the ledger's BUY handling
(``portfolio_analytics.domain.ledger._buy``) falls back to ``amount / quantity``
for the per-share cost — i.e. the user's stated total cost basis is preserved
exactly, with no separate fee line double-counted into it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from portfolio_analytics.broker_io.file_import import FileImportError
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import SYNC_FULL_REFRESH, ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalSecurity,
    synth_security_id,
)

SOURCE = "manual"
# One shared account per portfolio for every manually-entered position — mirrors
# csv_import.DEFAULT_ACCOUNT ("CSV"): a user adding a second manual ticker lands in
# the same "Manual" account rather than spawning a new one per position.
DEFAULT_ACCOUNT = "MANUAL"

# Tickers are 1-10 letters, optionally with a single '.' or '-' class suffix
# (e.g. "BRK.B", "BF-B") — permissive enough for real symbols, tight enough to
# reject obvious garbage ("", "12345", free text). Matches the shape csv_import
# already accepts implicitly (it never rejects a symbol beyond non-blank), just
# made explicit since this path has no file/row context to fall back on.
_TICKER_RE = re.compile(r"^[A-Z]{1,10}([.\-][A-Z]{1,4})?$")

# Upper bound on quantity/cost_basis magnitude — comfortably above any real position
# (a trillion shares/dollars), just tight enough to reject the ``Numeric(28, 10)``
# DB column overflowing at insert time (metron-ops#187 review) with a clean 422
# instead of an unhandled 500 mid-persist.
_MAX_MAGNITUDE = 1e15


class ManualEntryError(FileImportError):
    """The submitted manual position fields are invalid — 422, never silently dropped."""


def _validate_ticker(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if not symbol:
        raise ManualEntryError("Ticker is required.")
    if not _TICKER_RE.match(symbol):
        raise ManualEntryError(f"{raw!r} doesn't look like a valid ticker symbol.")
    return symbol


def _validate_number(value: float, *, field: str, allow_zero: bool) -> None:
    """Reject NaN/Infinity (both silently pass a bare ``<= 0`` comparison — ``NaN <=
    0`` and ``float("-inf") <= 0`` are both False/True in ways that dodge the
    "positive" check) and anything past ``_MAX_MAGNITUDE``, so a bad value 422s here
    rather than surfacing as an unhandled DB numeric-overflow 500 at insert time."""
    if not math.isfinite(value):
        raise ManualEntryError(f"{field.capitalize()} must be a finite number.")
    if abs(value) > _MAX_MAGNITUDE:
        raise ManualEntryError(f"{field.capitalize()} is too large.")
    if allow_zero and value < 0:
        raise ManualEntryError(f"{field.capitalize()} can't be negative.")
    if not allow_zero and value <= 0:
        raise ManualEntryError(f"{field.capitalize()} must be greater than zero.")


@dataclass(frozen=True)
class ManualPosition:
    """One user-entered stock/ETF position — the structured-field analog of a
    single parsed CSV BUY row."""

    ticker: str
    quantity: float
    cost_basis: float  # TOTAL cost basis for the position (not per-share)
    trade_date: date | None = None
    currency: str = "USD"
    account: str = DEFAULT_ACCOUNT


def build_manual_snapshot(entry: ManualPosition) -> ConnectorSnapshot:
    """Build a single-row ``ConnectorSnapshot`` for one manually-entered position.

    Raises ``ManualEntryError`` (422 at the router) on invalid input — a bad ticker,
    non-positive quantity, or negative cost basis. Mirrors ``csv_import``'s
    fail-loud-at-the-summary posture, just with nothing to skip past (a single
    entry has no "other rows" to protect)."""
    symbol = _validate_ticker(entry.ticker)
    _validate_number(entry.quantity, field="quantity", allow_zero=False)
    _validate_number(entry.cost_basis, field="cost basis", allow_zero=True)
    currency = (entry.currency or "USD").strip().upper() or "USD"
    when = entry.trade_date or date.today()
    acct_number = (entry.account or DEFAULT_ACCOUNT).strip() or DEFAULT_ACCOUNT

    security_id = synth_security_id(symbol, currency)
    security = CanonicalSecurity(security_id=security_id, ticker=symbol, name=symbol, currency=currency)
    account = CanonicalAccount(number=acct_number, label="Manual entry", source=SOURCE, currency=currency)
    activity = CanonicalActivity(
        account_number=acct_number,
        when=when,
        type=TxnType.BUY,
        security_id=security_id,
        quantity=entry.quantity,
        price=0.0,  # amount carries the user's stated total cost basis (see module docstring)
        amount=entry.cost_basis,
        fees=0.0,
        currency=currency,
        source=SOURCE,
    )
    return ConnectorSnapshot(
        source=SOURCE,
        accounts=[account],
        securities=[security],
        holdings=[],  # ledger-derived at read time, like CSV/OFX
        activities=[activity],
        sync_mode=SYNC_FULL_REFRESH,
    )
