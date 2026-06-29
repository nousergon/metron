"""Standalone crypto tracking (metron-ops#111).

A wallet-address-based view of crypto holdings, deliberately DECOUPLED from the broker /
EOD-close equities axis — crypto trades 24/7 with no market close, so it doesn't fit the
``price_bars`` / ``NavSnapshot`` daily grain. The user manages addresses here; Metron
publishes the deduped set to S3 (``data_spine.publish_wallet_addresses``) and the
``nousergon-data`` crypto-balances producer queries the chain and writes
``crypto/holdings.json`` back. This module reads that artifact and joins balances onto the
user's addresses — Metron itself makes NO chain calls (the data-spine invariant).

v1 scope: BTC + ETH. Adding a chain = a validator entry here + the producer's per-chain
adapter; the S3 artifact contract stays the same.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.services import data_spine

logger = logging.getLogger(__name__)

# Supported chains (v1). The value is the human label; the key is the stored ``chain``.
CHAINS = {"BTC": "Bitcoin", "ETH": "Ethereum"}

# Address validators — intentionally permissive on case (we never derive funds, just read a
# balance), strict on shape so a typo'd address is rejected at the UI rather than silently
# syncing to zero.
_BTC_RE = re.compile(r"^(bc1[a-z0-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,39})$")
_ETH_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class InvalidAddress(ValueError):
    """The address doesn't match the selected chain's format."""


def normalize_address(chain: str, address: str) -> tuple[str, str]:
    """Validate + canonicalize a ``(chain, address)`` pair, or raise :class:`InvalidAddress`.

    BTC addresses are case-sensitive (kept verbatim); ETH is hex — lower-cased so the same
    wallet typed in different cases dedupes to one row + one producer fetch."""
    ch = (chain or "").strip().upper()
    addr = (address or "").strip()
    if ch not in CHAINS:
        raise InvalidAddress(f"unsupported chain {chain!r} (supported: {', '.join(CHAINS)})")
    if not addr:
        raise InvalidAddress("address is required")
    if ch == "BTC":
        if not _BTC_RE.match(addr):
            raise InvalidAddress("not a valid BTC address")
        return ch, addr
    # ETH
    if not _ETH_RE.match(addr):
        raise InvalidAddress("not a valid ETH address (expected 0x + 40 hex chars)")
    return ch, addr.lower()


# ── Address CRUD (publishing the fetch universe on every change) ────────────────────────


def list_addresses(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[models.WalletAddress]:
    return list(
        session.scalars(
            select(models.WalletAddress)
            .where(
                models.WalletAddress.tenant_id == tenant_id,
                models.WalletAddress.portfolio_id == portfolio_id,
            )
            .order_by(models.WalletAddress.chain, models.WalletAddress.created_at)
        ).all()
    )


def add_address(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID,
    chain: str, address: str, *, label: str | None = None,
) -> models.WalletAddress:
    """Add (idempotent on chain+address) a wallet to track, then re-publish the producer's
    fetch universe. Re-adding an existing address updates only the label."""
    ch, addr = normalize_address(chain, address)
    row = session.scalars(
        select(models.WalletAddress).where(
            models.WalletAddress.tenant_id == tenant_id,
            models.WalletAddress.portfolio_id == portfolio_id,
            models.WalletAddress.chain == ch,
            models.WalletAddress.address == addr,
        )
    ).first()
    if row is None:
        row = models.WalletAddress(
            tenant_id=tenant_id, portfolio_id=portfolio_id, chain=ch, address=addr,
            label=(label or "").strip() or None,
        )
        session.add(row)
    else:
        row.label = (label or "").strip() or None
    session.commit()
    session.refresh(row)
    _publish(session)
    return row


def delete_address(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, address_id: uuid.UUID) -> bool:
    row = session.get(models.WalletAddress, address_id)
    if row is None or row.tenant_id != tenant_id or row.portfolio_id != portfolio_id:
        return False
    session.delete(row)
    session.commit()
    _publish(session)
    return True


def _publish(session: Session) -> None:
    """Best-effort re-publish of the wallet-address fetch universe. Fail-soft — a publish
    failure WARNs (the producer just keeps its prior universe one cycle) and must never break
    the user's add/delete, which has already committed."""
    try:
        data_spine.publish_wallet_addresses(session)
    except data_spine.DataSpineUnavailable as e:
        logger.warning("wallet-address publish failed (producer keeps prior universe): %s", e)


# ── Synced balances (read the producer's crypto/holdings.json) ──────────────────────────

_STALE_AFTER_SECONDS = 60 * 60  # 1h — crypto is 24/7; older than this is a stalled producer
_SNAPSHOT_TTL_S = 60.0
_cache: dict | None = None
_cache_fetched_monotonic = -1e9
_cache_lock = threading.Lock()


def _read_holdings_s3() -> dict | None:
    import boto3
    try:
        obj = boto3.client("s3").get_object(
            Bucket=settings.market_data_bucket, Key=data_spine.CRYPTO_HOLDINGS_KEY
        )
        return json.loads(obj["Body"].read())
    except Exception as e:  # noqa: BLE001 - read is best-effort; absence → "pending", not a 500
        logger.warning("crypto holdings read failed %s: %s", data_spine.CRYPTO_HOLDINGS_KEY, e)
        return None


def _default_reader() -> dict | None:
    global _cache, _cache_fetched_monotonic
    with _cache_lock:
        if time.monotonic() - _cache_fetched_monotonic < _SNAPSHOT_TTL_S:
            return _cache
        _cache = _read_holdings_s3()
        _cache_fetched_monotonic = time.monotonic()
        return _cache


@dataclass
class CryptoPosition:
    id: uuid.UUID             # the WalletAddress row id — the delete handle for the UI
    chain: str
    address: str
    label: str | None
    symbol: str | None        # "BTC" / "ETH" once synced
    balance: float | None     # native units; None = not yet synced
    price_usd: float | None
    value_usd: float | None
    synced: bool              # a fresh balance is present for this address


@dataclass
class CryptoSummary:
    available: bool                 # the producer artifact exists + is fresh
    as_of_utc: str | None = None
    stale: bool = False
    total_usd: float | None = None  # sum over synced positions
    n_pending: int = 0              # tracked addresses still awaiting a first sync
    positions: list[CryptoPosition] = None  # type: ignore[assignment]
    reason: str | None = None       # "unavailable" / "stale" when not available

    def __post_init__(self):
        if self.positions is None:
            self.positions = []


def _is_stale(as_of: str | None, now: datetime) -> bool:
    if not as_of:
        return True
    try:
        beat = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - beat).total_seconds() > _STALE_AFTER_SECONDS


def for_portfolio(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID,
    *, reader=None, now: datetime | None = None,
) -> CryptoSummary:
    """The portfolio's tracked wallets joined with the producer's synced balances. Addresses
    without a fresh balance render as ``synced=False`` ("pending first sync") — never zeroed.
    The artifact being absent/stale doesn't hide the user's address list; it just marks every
    position pending and flags the summary."""
    now = now or datetime.now(UTC)
    addrs = list_addresses(session, tenant_id, portfolio_id)
    art = (reader or _default_reader)()
    by_addr: dict[tuple[str, str], dict] = {}
    as_of = None
    if art:
        as_of = art.get("as_of_utc")
        for b in art.get("balances", []):
            by_addr[(str(b.get("chain", "")).upper(), str(b.get("address", "")))] = b
    stale = _is_stale(as_of, now)
    available = bool(art) and not stale
    positions: list[CryptoPosition] = []
    n_pending = 0
    total = 0.0
    any_value = False
    for a in addrs:
        b = by_addr.get((a.chain, a.address)) if available else None
        if b is None:
            n_pending += 1
            positions.append(
                CryptoPosition(a.id, a.chain, a.address, a.label, None, None, None, None, synced=False)
            )
            continue
        bal = _f(b.get("balance"))
        px = _f(b.get("price_usd"))
        val = _f(b.get("value_usd"))
        if val is None and bal is not None and px is not None:
            val = bal * px
        if val is not None:
            total += val
            any_value = True
        positions.append(
            CryptoPosition(a.id, a.chain, a.address, a.label, b.get("symbol") or a.chain, bal, px, val, synced=True)
        )
    return CryptoSummary(
        available=available,
        as_of_utc=as_of,
        stale=stale,
        total_usd=total if any_value else None,
        n_pending=n_pending,
        positions=positions,
        reason=None if available else ("stale" if art else "unavailable"),
    )


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def record_snapshot(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, summary: CryptoSummary,
    *, today: date | None = None,
) -> models.CryptoValueSnapshot | None:
    """Forward-record today's total crypto value (idempotent per UTC day). Skipped — never
    fabricated — when there's no fresh total. Mirrors ``NavSnapshot`` accrual: crypto value
    history can't be backfilled, so it accrues one day at a time as the page is viewed."""
    if summary.total_usd is None or not summary.available:
        return None
    day = today or datetime.now(UTC).date()
    existing = session.scalars(
        select(models.CryptoValueSnapshot).where(
            models.CryptoValueSnapshot.tenant_id == tenant_id,
            models.CryptoValueSnapshot.portfolio_id == portfolio_id,
            models.CryptoValueSnapshot.snap_date == day,
        )
    ).first()
    if existing is not None:
        existing.value_usd = summary.total_usd
    else:
        existing = models.CryptoValueSnapshot(
            tenant_id=tenant_id, portfolio_id=portfolio_id, snap_date=day, value_usd=summary.total_usd
        )
        session.add(existing)
    session.commit()
    return existing
