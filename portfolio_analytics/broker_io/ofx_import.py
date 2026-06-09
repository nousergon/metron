"""OFX/QFX transaction import — the second free-tier ingestion path.

OFX (Open Financial Exchange) is the format behind "Download to Quicken/Money" at
virtually every brokerage; QFX is Intuit's near-identical variant. Supporting it
means a user with no CSV export — but the ubiquitous OFX download — still round-trips
into Metron with no token and no paid aggregator (commercialization plan §6 PH1).

Parsing is delegated to **ofxtools** (spec-strict OFX SGML/XML), then the investment
statement is normalized into the *same* canonical ``ConnectorSnapshot`` the CSV
importer emits — so OFX lands through the identical persistence bridge and read path,
adding a source without adding a schema.

Posture matches the CSV importer (``file_import``): an unrecognized or malformed
single transaction is collected as a ``SkippedRecord`` (``ref="fitid …"``) and never
silently dropped; a file with no parseable investment statement raises
``OfxImportError`` (→ HTTP 422). Securities are resolved CUSIP/uniqueid → ticker via
the file's SECLIST, falling back to the raw id so a holding is never lost to a
missing master entry.
"""

from __future__ import annotations

import io
from datetime import date

from ofxtools.Parser import OFXTree

from portfolio_analytics.broker_io.file_import import FileImportError, FileImportResult, SkippedRecord
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.base import SYNC_FULL_REFRESH, BrokerConnector, ConnectorSnapshot
from portfolio_analytics.ingestion.schema import (
    CanonicalAccount,
    CanonicalActivity,
    CanonicalSecurity,
    synth_security_id,
)

SOURCE = "ofx"

# OFX INCOME.INCOMETYPE → canonical type. Capital-gain distributions (CGLONG/CGSHORT)
# and MISC are cash income, mapped to DIVIDEND; explicit interest stays INTEREST.
_INCOME_TYPE = {"INTEREST": TxnType.INTEREST}

# OFX bank TRNTYPE → canonical type, when the type alone is decisive. Everything else
# falls back to the signed amount (positive = DEPOSIT, negative = WITHDRAWAL).
_BANK_TYPE = {
    "INT": TxnType.INTEREST,
    "DIV": TxnType.DIVIDEND,
    "FEE": TxnType.FEE,
    "SRVCHG": TxnType.FEE,
}


class OfxImportError(FileImportError):
    """The OFX file is structurally unusable (not OFX, or no investment statement)."""


def _f(value) -> float:
    """Decimal/None → float magnitude (sign is carried by the canonical type)."""
    return abs(float(value)) if value is not None else 0.0


def _signed(value) -> float:
    return float(value) if value is not None else 0.0


def _as_date(value) -> date | None:
    return value.date() if value is not None else None


def _security_map(ofx) -> dict[str, CanonicalSecurity]:
    """Build uniqueid → CanonicalSecurity from the file's SECLIST."""
    out: dict[str, CanonicalSecurity] = {}
    for sec in getattr(ofx, "securities", None) or []:
        uniqueid = getattr(sec, "uniqueid", None)
        if not uniqueid:
            continue
        ticker = (getattr(sec, "ticker", None) or "").upper()
        currency = getattr(sec, "currency", None) or "USD"
        symbol = ticker or str(uniqueid)
        out[str(uniqueid)] = CanonicalSecurity(
            security_id=synth_security_id(symbol, currency),
            ticker=symbol,
            name=getattr(sec, "secname", "") or symbol,
            currency=currency,
        )
    return out


def _resolve_security(uniqueid, sec_map: dict[str, CanonicalSecurity], currency: str) -> CanonicalSecurity | None:
    """Resolve a transaction's security via the SECLIST, falling back to its raw id so
    a holding is never dropped for a missing master entry."""
    if not uniqueid:
        return None
    key = str(uniqueid)
    if key in sec_map:
        return sec_map[key]
    # Unknown id: track it by the raw identifier rather than losing the position.
    return CanonicalSecurity(security_id=synth_security_id(key, currency), ticker=key, name=key, currency=currency)


def _classify(tr) -> TxnType | None:
    """Map an ofxtools investment-transaction aggregate to a canonical ``TxnType``.

    Returns ``None`` for a transaction kind this importer does not model (transfers,
    splits, journals, option assignments) — the caller records it as skipped.
    """
    name = type(tr).__name__
    if name == "INVBANKTRAN":
        trntype = getattr(tr, "trntype", None)
        if trntype in _BANK_TYPE:
            return _BANK_TYPE[trntype]
        amt = _signed(getattr(tr, "trnamt", None))
        if amt > 0:
            return TxnType.DEPOSIT
        if amt < 0:
            return TxnType.WITHDRAWAL
        return None
    if name == "INCOME":
        return _INCOME_TYPE.get(getattr(tr, "incometype", None), TxnType.DIVIDEND)
    if name == "REINVEST":
        return TxnType.BUY  # dividend reinvested into shares
    if getattr(tr, "buytype", None) is not None or name.startswith("BUY"):
        return TxnType.BUY
    if getattr(tr, "selltype", None) is not None or name.startswith("SELL"):
        return TxnType.SELL
    return None


def _activity_from(
    tr, ttype: TxnType, account_number: str, sec_map, currency: str
) -> tuple[CanonicalActivity, CanonicalSecurity | None]:
    """Build the canonical activity + the security it references (None for cash)."""
    is_cash_bank = type(tr).__name__ == "INVBANKTRAN"
    when = _as_date(getattr(tr, "dtposted", None) if is_cash_bank else getattr(tr, "dttrade", None))
    if when is None:
        raise ValueError("transaction has no trade/posted date")

    security = None if is_cash_bank else _resolve_security(getattr(tr, "uniqueid", None), sec_map, currency)
    fees = _f(getattr(tr, "commission", None)) + _f(getattr(tr, "fees", None))
    amount = _f(getattr(tr, "trnamt", None)) if is_cash_bank else _f(getattr(tr, "total", None))
    activity = CanonicalActivity(
        account_number=account_number,
        when=when,
        type=ttype,
        security_id=security.security_id if security is not None else "",
        quantity=_f(getattr(tr, "units", None)),
        price=_f(getattr(tr, "unitprice", None)),
        amount=amount,
        fees=fees,
        currency=currency,
        source=SOURCE,
    )
    return activity, security


def parse_ofx(data: str | bytes) -> FileImportResult:
    """Parse an OFX/QFX file into a canonical ``ConnectorSnapshot``.

    Raises ``OfxImportError`` if the bytes are not parseable OFX or contain no
    investment statement; per-transaction failures are collected, not raised.
    """
    raw = data.encode("utf-8", "ignore") if isinstance(data, str) else data
    if not raw.strip():
        # ofxtools' header parser blocks on empty input — reject it before handing over.
        raise OfxImportError("OFX file is empty")
    try:
        tree = OFXTree()
        tree.parse(io.BytesIO(raw))
        ofx = tree.convert()
    except Exception as e:  # noqa: BLE001 — any ofxtools failure is a user-facing 422; surfaced, not swallowed
        raise OfxImportError(f"could not parse OFX file: {e}") from e

    statements = [st for st in getattr(ofx, "statements", []) if hasattr(st, "transactions")]
    if not statements:
        raise OfxImportError("OFX file contains no investment statement")

    sec_map = _security_map(ofx)
    referenced: dict[str, CanonicalSecurity] = {}
    accounts: dict[str, CanonicalAccount] = {}
    activities: list[CanonicalActivity] = []
    errors: list[SkippedRecord] = []

    for st in statements:
        account_number = str(st.account.acctid)
        currency = getattr(st, "curdef", None) or "USD"
        institution = str(getattr(st.account, "brokerid", "") or "")
        accounts.setdefault(
            account_number,
            CanonicalAccount(number=account_number, label=account_number, institution=institution,
                             currency=currency, source=SOURCE),
        )
        for tr in st.transactions:
            ref = f"fitid {getattr(tr, 'fitid', '?')}"
            ttype = _classify(tr)
            if ttype is None:
                errors.append(SkippedRecord(ref=ref, reason=f"unsupported transaction type {type(tr).__name__}"))
                continue
            try:
                act, security = _activity_from(tr, ttype, account_number, sec_map, currency)
            except ValueError as e:
                errors.append(SkippedRecord(ref=ref, reason=str(e)))
                continue
            if security is not None:
                referenced[security.security_id] = security
            activities.append(act)

    snapshot = ConnectorSnapshot(
        source=SOURCE,
        accounts=list(accounts.values()),
        securities=list(referenced.values()),
        holdings=[],  # positions are ledger-derived at read time
        activities=activities,
        sync_mode=SYNC_FULL_REFRESH,
    )
    return FileImportResult(snapshot=snapshot, errors=errors, parsed=len(activities), skipped=len(errors))


class OfxConnector(BrokerConnector):
    """``BrokerConnector`` wrapper so a parsed OFX file flows through ``ingest()`` like
    any broker source. ``sync`` does no network I/O and never raises — a structural
    failure degrades to an empty snapshot with ``error`` set."""

    source = SOURCE

    def __init__(self, data: str | bytes) -> None:
        self._data = data
        self.result: FileImportResult | None = None

    def sync(self, state: dict | None = None) -> ConnectorSnapshot:
        try:
            self.result = parse_ofx(self._data)
            return self.result.snapshot
        except OfxImportError as e:
            return ConnectorSnapshot(source=self.source, error=str(e))
