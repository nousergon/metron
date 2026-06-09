"""Bronze + silver persistence for the connector ingestion layer.

**Bronze** (``cache/connectors/bronze/<source>/``) — the raw broker payload exactly
as fetched (Flex XML, SnapTrade JSON) plus an append-only provenance manifest. The
replay/audit landing zone; never parsed back into the app, kept for debugging and
lineage.

**Silver** (``cache/connectors/silver.json``) — the merged canonical store the
dashboard reads. Persisted under gitignored ``cache/`` so it survives deploys.

Merge semantics fork by record type (the institutional event-vs-snapshot split):
  * ``accounts`` / ``holdings`` — point-in-time **snapshots**: last-write-wins per
    account on a successful sync (a closed-out account's holdings must be replaceable,
    so they're set per account, not unioned — else positions ghost forever).
  * ``securities`` — the instrument master: **upsert** by ``security_id``.
  * ``activities`` / ``realized_lots`` — immutable **events**: union/append by a
    stable key, so history accumulates beyond a broker's rolling window and a
    transient fetch failure never drops previously-seen rows.

Pure stdlib; no Streamlit. ``opener``-free (no network) — fully unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from portfolio_analytics.domain.ledger import RealizedGain, TxnType
from portfolio_analytics.ingestion.schema import (
    SCHEMA_VERSION,
    CanonicalAccount,
    CanonicalActivity,
    CanonicalHolding,
    CanonicalSecurity,
    activity_key,
    lot_key,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path("cache/connectors")
SILVER_PATH = CACHE_DIR / "silver.json"
BRONZE_DIR = CACHE_DIR / "bronze"


# ── serialization helpers ───────────────────────────────────────────────────
def _iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d is not None else None


def _as_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _as_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _account_row(a: CanonicalAccount) -> dict:
    return {
        "number": a.number,
        "label": a.label,
        "institution": a.institution,
        "tax_treatment": a.tax_treatment,
        "nav_usd": a.nav_usd,
        "cash_usd": a.cash_usd,
        "currency": a.currency,
        "as_of": _iso(a.as_of),
        "source": a.source,
        "account_id": a.account_id,
        "name": a.name,
        "account_type": a.account_type,
    }


def _account_obj(r: dict) -> CanonicalAccount:
    return CanonicalAccount(
        number=r["number"],
        label=r.get("label", ""),
        institution=r.get("institution", ""),
        tax_treatment=r.get("tax_treatment", ""),
        nav_usd=float(r.get("nav_usd", 0.0)),
        cash_usd=float(r.get("cash_usd", 0.0)),
        currency=r.get("currency", "USD"),
        as_of=_as_dt(r.get("as_of")),
        source=r.get("source", ""),
        account_id=r.get("account_id", ""),
        name=r.get("name", ""),
        account_type=r.get("account_type", ""),
    )


def _security_row(s: CanonicalSecurity) -> dict:
    return {
        "security_id": s.security_id,
        "ticker": s.ticker,
        "name": s.name,
        "currency": s.currency,
        "asset_type": s.asset_type,
        "security_id_type": s.security_id_type,
    }


def _security_obj(r: dict) -> CanonicalSecurity:
    return CanonicalSecurity(
        security_id=r["security_id"],
        ticker=r.get("ticker", ""),
        name=r.get("name", ""),
        currency=r.get("currency", "USD"),
        asset_type=r.get("asset_type", "EQUITY"),
        security_id_type=r.get("security_id_type", "TICKER"),
    )


def _holding_row(h: CanonicalHolding) -> dict:
    return {
        "account_number": h.account_number,
        "security_id": h.security_id,
        "quantity": h.quantity,
        "cost_basis": h.cost_basis,
        "avg_cost": h.avg_cost,
        "market_value_local": h.market_value_local,
        "currency": h.currency,
        "as_of": _iso(h.as_of),
        "source": h.source,
    }


def _holding_obj(r: dict) -> CanonicalHolding:
    return CanonicalHolding(
        account_number=r["account_number"],
        security_id=r["security_id"],
        quantity=float(r.get("quantity", 0.0)),
        cost_basis=float(r.get("cost_basis", 0.0)),
        avg_cost=float(r.get("avg_cost", 0.0)),
        market_value_local=float(r.get("market_value_local", 0.0)),
        currency=r.get("currency", "USD"),
        as_of=_as_dt(r.get("as_of")),
        source=r.get("source", ""),
    )


def _activity_row(a: CanonicalActivity) -> dict:
    return {
        "account_number": a.account_number,
        "when": _iso(a.when),
        "type": str(a.type),
        "security_id": a.security_id,
        "quantity": a.quantity,
        "price": a.price,
        "amount": a.amount,
        "fees": a.fees,
        "currency": a.currency,
        "as_of": _iso(a.as_of),
        "source": a.source,
    }


def _activity_obj(r: dict) -> CanonicalActivity:
    return CanonicalActivity(
        account_number=r["account_number"],
        when=_as_date(r["when"]),
        type=TxnType(r["type"]),
        security_id=r.get("security_id", ""),
        quantity=float(r.get("quantity", 0.0)),
        price=float(r.get("price", 0.0)),
        amount=float(r.get("amount", 0.0)),
        fees=float(r.get("fees", 0.0)),
        currency=r.get("currency", "USD"),
        as_of=_as_dt(r.get("as_of")),
        source=r.get("source", ""),
    )


def _lot_row(account_number: str, rg: RealizedGain) -> dict:
    return {
        "account_number": account_number,
        "ticker": rg.ticker,
        "open_date": _iso(rg.open_date),
        "close_date": _iso(rg.close_date),
        "quantity": rg.quantity,
        "proceeds": rg.proceeds,
        "cost_basis": rg.cost_basis,
    }


def _lot_obj(r: dict) -> tuple[str, RealizedGain]:
    return (
        r.get("account_number", ""),
        RealizedGain(
            ticker=r["ticker"],
            open_date=_as_date(r["open_date"]),
            close_date=_as_date(r["close_date"]),
            quantity=float(r["quantity"]),
            proceeds=float(r["proceeds"]),
            cost_basis=float(r["cost_basis"]),
        ),
    )


# ── the silver canonical store ──────────────────────────────────────────────
@dataclass
class CanonicalStore:
    """In-memory silver store. Keyed containers enforce the per-record semantics."""

    accounts: dict[str, CanonicalAccount] = field(default_factory=dict)  # by number
    securities: dict[str, CanonicalSecurity] = field(default_factory=dict)  # by security_id
    holdings: dict[str, list[CanonicalHolding]] = field(default_factory=dict)  # by account_number
    activities: dict[str, CanonicalActivity] = field(default_factory=dict)  # by activity_key
    realized_lots: dict[str, tuple[str, RealizedGain]] = field(default_factory=dict)  # by lot_key
    meta: dict[str, dict] = field(default_factory=dict)  # by source

    # --- merge a single connector's already-ownership-filtered records ---
    def merge(
        self,
        source: str,
        accounts: list[CanonicalAccount],
        securities: list[CanonicalSecurity],
        holdings: list[CanonicalHolding],
        activities: list[CanonicalActivity],
        realized_lots: list[tuple[str, RealizedGain]],
        as_of: datetime | None = None,
    ) -> None:
        """Apply one source's records. ``accounts`` are authoritative for this source:
        their holdings are REPLACED per account (clearing stale positions), while
        securities upsert and activities/lots union."""
        replace_set = {a.number for a in accounts}
        for a in accounts:
            self.accounts[a.number] = a
        for s in securities:
            self.securities[s.security_id] = s  # upsert master
        # replace holdings for every authoritative account (empty list clears it)
        grouped: dict[str, list[CanonicalHolding]] = {n: [] for n in replace_set}
        for h in holdings:
            grouped.setdefault(h.account_number, []).append(h)
        for n in replace_set:
            self.holdings[n] = grouped.get(n, [])
        for act in activities:
            self.activities.setdefault(activity_key(act), act)  # union (first wins)
        for number, rg in realized_lots:
            self.realized_lots.setdefault(lot_key(number, rg), (number, rg))  # union
        self.meta[source] = {"as_of": _iso(as_of), "synced_at": _iso(datetime.now())}

    # --- typed read accessors (the gold/serving layer reads these) ---
    def all_accounts(self) -> list[CanonicalAccount]:
        return list(self.accounts.values())

    def all_holdings(self) -> list[CanonicalHolding]:
        return [h for hs in self.holdings.values() for h in hs]

    def all_activities(self) -> list[CanonicalActivity]:
        return list(self.activities.values())

    def all_realized_lots(self) -> list[tuple[str, RealizedGain]]:
        return list(self.realized_lots.values())

    def security(self, security_id: str) -> CanonicalSecurity | None:
        return self.securities.get(security_id)


def load_store(path: Path = SILVER_PATH) -> CanonicalStore:
    """Load the silver store; an empty store if absent, and a best-effort partial
    load (skip malformed rows with a WARN) if the file is corrupt — never blank the
    whole store on one bad row."""
    store = CanonicalStore()
    if not Path(path).exists():
        return store
    try:
        data = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read canonical store %s: %s — starting empty", path, e)
        return store
    for r in data.get("accounts", []):
        try:
            obj = _account_obj(r)
            store.accounts[obj.number] = obj
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed account row %r: %s", r, e)
    for r in data.get("securities", []):
        try:
            obj = _security_obj(r)
            store.securities[obj.security_id] = obj
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed security row %r: %s", r, e)
    for r in data.get("holdings", []):
        try:
            obj = _holding_obj(r)
            store.holdings.setdefault(obj.account_number, []).append(obj)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed holding row %r: %s", r, e)
    for r in data.get("activities", []):
        try:
            obj = _activity_obj(r)
            store.activities[activity_key(obj)] = obj
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed activity row %r: %s", r, e)
    for r in data.get("realized_lots", []):
        try:
            number, rg = _lot_obj(r)
            store.realized_lots[lot_key(number, rg)] = (number, rg)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Skipping malformed lot row %r: %s", r, e)
    store.meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
    return store


def save_store(store: CanonicalStore, path: Path = SILVER_PATH) -> None:
    """Persist the silver store atomically (write-temp-then-replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "accounts": [_account_row(a) for a in store.accounts.values()],
        "securities": [_security_row(s) for s in store.securities.values()],
        "holdings": [_holding_row(h) for hs in store.holdings.values() for h in hs],
        "activities": [_activity_row(a) for a in store.activities.values()],
        "realized_lots": [_lot_row(n, rg) for n, rg in store.realized_lots.values()],
        "meta": store.meta,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


# ── bronze raw-payload landing zone ─────────────────────────────────────────
def save_bronze(
    source: str,
    payload: str | bytes,
    *,
    bronze_dir: Path = BRONZE_DIR,
    fetched_at: datetime | None = None,
    batch_id: str | None = None,
) -> dict | None:
    """Land a raw broker payload + append a provenance manifest line. Returns the
    manifest entry, or None if the bronze write failed.

    Bronze is secondary observability hung off the primary ingest path: a write
    failure here must NOT blank the live data the silver merge produced, so it's
    caught — but it's recorded (WARN log) so the failure is never silent."""
    fetched_at = fetched_at or datetime.now()
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    entry = {
        "source": source,
        "fetched_at": fetched_at.isoformat(),
        "batch_id": batch_id or fetched_at.strftime("%Y%m%dT%H%M%S"),
        "bytes": len(data),
        "sha1": hashlib.sha1(data).hexdigest(),  # noqa: S324 — content fingerprint, not security
    }
    try:
        src_dir = Path(bronze_dir) / source
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "latest.raw").write_bytes(data)
        with (src_dir / "_manifest.ndjson").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry
    except OSError as e:
        # (a) disk/permission failure landing the audit copy; (c) recorded here as a
        # WARN — the silver merge already has the parsed data, so ingest proceeds.
        logger.warning("Bronze write failed for %s: %s", source, e)
        return None
