"""CSV transaction import — the universal free-tier ingestion path.

Every broker exports a transactions/activity CSV, so a flexible CSV importer is the
one connector that works for *any* user without a token, an API key, or a paid
aggregator — it is the substrate behind the free beta's "export a CSV, see your
portfolio" promise (commercialization plan §6 PH1, gate: *a stranger's exported CSV
round-trips to a correct Portfolio page*).

The importer normalizes a header-flexible CSV into the same canonical records every
other connector emits (``ConnectorSnapshot`` of ``CanonicalAccount`` /
``CanonicalSecurity`` / ``CanonicalActivity``), so it joins the ingestion pipeline
on equal footing with IBKR Flex and SnapTrade — one downstream schema regardless of
source.

**Posture on bad rows (fail-loud at the summary).** A user-uploaded file is dirty by
nature; a row that cannot be parsed is *not* silently dropped. Every skipped row is
recorded with its 1-based line number and a human reason in
``CsvImportResult.errors`` so the import endpoint can show the user exactly what did
not import. The parse itself never raises on a single bad row (one typo must not
reject a 500-row file); a structurally unusable file — missing the required ``date``
or ``type`` column — *does* raise ``CsvImportError`` (the whole import is invalid).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from portfolio_analytics.broker_io.file_import import FileImportError, FileImportResult, SkippedRecord
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import SYNC_FULL_REFRESH, BrokerConnector, ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalSecurity,
    synth_security_id,
)

SOURCE = "csv"
DEFAULT_ACCOUNT = "CSV"

# Header aliases → canonical field. Matching is case-insensitive on the stripped
# header. The first alias present in the file wins for each canonical field.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "trade_date", "trade date", "settle_date", "settlement date", "when", "as of date"),
    "type": ("type", "action", "activity", "transaction type", "transaction", "description type"),
    "symbol": ("symbol", "ticker", "security", "instrument"),
    "quantity": ("quantity", "qty", "shares", "units", "amount of shares"),
    "price": ("price", "trade_price", "price per share", "unit price"),
    "amount": ("amount", "value", "net amount", "total", "net cash"),
    "fees": ("fees", "fee", "commission", "commissions", "comm"),
    "currency": ("currency", "ccy", "currency code"),
    "account": ("account", "account_number", "account number", "acct", "account id"),
}

# Free-text transaction-type synonyms → canonical ``TxnType``. Lower-cased, stripped.
_TYPE_SYNONYMS: dict[str, TxnType] = {
    "buy": TxnType.BUY,
    "bought": TxnType.BUY,
    "purchase": TxnType.BUY,
    "buy to open": TxnType.BUY,
    "reinvestment": TxnType.BUY,
    "reinvest shares": TxnType.BUY,
    "sell": TxnType.SELL,
    "sold": TxnType.SELL,
    "sale": TxnType.SELL,
    "sell to close": TxnType.SELL,
    "dividend": TxnType.DIVIDEND,
    "div": TxnType.DIVIDEND,
    "qualified dividend": TxnType.DIVIDEND,
    "cash dividend": TxnType.DIVIDEND,
    "interest": TxnType.INTEREST,
    "credit interest": TxnType.INTEREST,
    "deposit": TxnType.DEPOSIT,
    "contribution": TxnType.DEPOSIT,
    "transfer in": TxnType.DEPOSIT,
    "funds received": TxnType.DEPOSIT,
    "withdrawal": TxnType.WITHDRAWAL,
    "withdraw": TxnType.WITHDRAWAL,
    "transfer out": TxnType.WITHDRAWAL,
    "distribution": TxnType.WITHDRAWAL,
    "fee": TxnType.FEE,
    "commission": TxnType.FEE,
    "advisory fee": TxnType.FEE,
    "split": TxnType.SPLIT,
    "stock split": TxnType.SPLIT,
    "forward split": TxnType.SPLIT,
}

# Types that require a non-empty symbol; cash events (deposit/withdrawal/fee/interest)
# do not. DIVIDEND keeps its symbol when present but tolerates a blank (cash sweep).
_SECURITY_REQUIRED = {TxnType.BUY, TxnType.SELL, TxnType.SPLIT}


class CsvImportError(FileImportError):
    """The CSV is structurally unusable (missing a required column / no header)."""


def _normalize_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map each canonical field to the actual header present in the file (or absent)."""
    present = {(h or "").strip().lower(): h for h in fieldnames}
    resolved: dict[str, str] = {}
    for canonical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if alias in present:
                resolved[canonical] = present[alias]
                break
    return resolved


def _num(value: str | None) -> float:
    """Parse a money/quantity cell, tolerating ``$``, thousands commas, and parens
    for negatives. Empty → 0.0. Raises ``ValueError`` on genuine garbage."""
    if value is None:
        return 0.0
    s = value.strip()
    if not s:
        return 0.0
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    if not s:
        return 0.0
    out = float(s)
    return -out if negative else out


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%d/%m/%Y")


def _parse_date(value: str | None) -> date:
    """Parse a date cell across the common broker formats. Raises on failure."""
    s = (value or "").strip()
    if not s:
        raise ValueError("empty date")
    # ISO datetime (e.g. SnapTrade-style "2024-01-15T00:00:00Z") → take the date part.
    head = s.split("T", 1)[0].split(" ", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {value!r}")


def _coerce_type(value: str | None) -> TxnType:
    """Map a free-text type cell to a canonical ``TxnType``. Raises on unknown."""
    s = (value or "").strip().lower()
    if not s:
        raise ValueError("empty type")
    if s in _TYPE_SYNONYMS:
        return _TYPE_SYNONYMS[s]
    # Tolerate already-canonical values (BUY/SELL/…) regardless of case.
    try:
        return TxnType(s.upper())
    except ValueError as e:
        raise ValueError(f"unknown transaction type {value!r}") from e


def parse_transactions_csv(
    text: str,
    *,
    default_account: str = DEFAULT_ACCOUNT,
    source: str = SOURCE,
) -> FileImportResult:
    """Parse a transactions CSV into a canonical ``ConnectorSnapshot``.

    Header-flexible (see ``_HEADER_ALIASES``); ``date`` and ``type`` columns are
    mandatory. Per-row failures are collected into ``FileImportResult.errors`` rather
    than raised, so one bad row never rejects the file; a missing required column
    raises ``CsvImportError``.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("CSV has no header row")
    cols = _normalize_headers(list(reader.fieldnames))
    for required in ("date", "type"):
        if required not in cols:
            raise CsvImportError(f"CSV is missing a required '{required}' column")

    accounts: dict[str, CanonicalAccount] = {}
    securities: dict[str, CanonicalSecurity] = {}
    activities: list[CanonicalActivity] = []
    errors: list[SkippedRecord] = []

    def cell(row: dict, field_: str) -> str | None:
        header = cols.get(field_)
        return row.get(header) if header else None

    # csv.DictReader yields data rows starting at file line 2 (line 1 is the header).
    for offset, row in enumerate(reader, start=2):
        try:
            ttype = _coerce_type(cell(row, "type"))
            when = _parse_date(cell(row, "date"))
            symbol = (cell(row, "symbol") or "").strip().upper()
            currency = (cell(row, "currency") or "USD").strip().upper() or "USD"
            qty = abs(_num(cell(row, "quantity")))
            price = abs(_num(cell(row, "price")))
            amount = abs(_num(cell(row, "amount")))
            fees = abs(_num(cell(row, "fees")))
            acct_number = (cell(row, "account") or "").strip() or default_account

            if ttype in _SECURITY_REQUIRED and not symbol:
                raise ValueError(f"{ttype.value} row requires a symbol")

            security_id = ""
            if symbol:
                security_id = synth_security_id(symbol, currency)
                securities.setdefault(
                    security_id,
                    CanonicalSecurity(security_id=security_id, ticker=symbol, name=symbol, currency=currency),
                )

            accounts.setdefault(
                acct_number,
                CanonicalAccount(number=acct_number, label=acct_number, source=source, currency=currency),
            )
            activities.append(
                CanonicalActivity(
                    account_number=acct_number,
                    when=when,
                    type=ttype,
                    security_id=security_id,
                    quantity=qty,
                    price=price,
                    amount=amount,
                    fees=fees,
                    currency=currency,
                    source=source,
                )
            )
        except ValueError as e:
            errors.append(SkippedRecord(ref=f"line {offset}", reason=str(e), raw=dict(row)))

    snapshot = ConnectorSnapshot(
        source=source,
        accounts=list(accounts.values()),
        securities=list(securities.values()),
        holdings=[],  # positions are ledger-derived at read time, never stored from a CSV
        activities=activities,
        sync_mode=SYNC_FULL_REFRESH,
    )
    return FileImportResult(snapshot=snapshot, errors=errors, parsed=len(activities), skipped=len(errors))


class CsvConnector(BrokerConnector):
    """``BrokerConnector`` wrapper so a parsed CSV can flow through ``ingest()`` like
    any other source. Constructed from already-uploaded file text — ``sync`` does no
    I/O and never raises (a parse failure surfaces as an empty snapshot + errors on
    the held ``result``)."""

    source = SOURCE

    def __init__(self, text: str, *, default_account: str = DEFAULT_ACCOUNT) -> None:
        self._text = text
        self._default_account = default_account
        self.result: FileImportResult | None = None

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        try:
            self.result = parse_transactions_csv(self._text, default_account=self._default_account)
            return self.result.snapshot
        except CsvImportError as e:
            return ConnectorSnapshot(source=self.source, error=str(e))
