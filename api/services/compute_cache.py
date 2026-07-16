"""Process-level single-flight cache for expensive read computations.

A dashboard page fans out into several parallel API requests that each recompute the
same costly per-portfolio aggregates (NAV reconstruction, valued holdings). This caches
those results keyed by a **content fingerprint** of the portfolio's mutable state, so:

- a cache entry is valid for *exactly* as long as the underlying data is unchanged — a
  fingerprint mismatch (after any import / price refresh / account change) misses and
  recomputes, so there is **no staleness window** (unlike a pure TTL cache); and
- concurrent callers within one process share a single computation via a per-key lock
  (single-flight) — no thundering-herd recompute when six requests land at once.

The TTL here is only a memory-eviction backstop (bound the number of retained
fingerprints), NOT a correctness mechanism. In-process only (one uvicorn worker on the
owner build); a future multi-tenant deployment swaps this for a shared cache behind the
same `cached()` seam. Errors are never cached — a failed compute raises to the caller
(fail-loud) and leaves the slot empty so the next request retries."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models

T = TypeVar("T")

_TTL_S = 300.0  # memory-eviction backstop only; correctness comes from the fingerprint key
_lock = threading.Lock()  # guards the registry + per-key lock creation
_key_locks: dict[str, threading.Lock] = {}
_entries: dict[str, tuple[float, object]] = {}  # key -> (stored_monotonic, value)


def _term(session: Session, model, tenant_id, portfolio_id) -> str:
    """A `(count, freshest-timestamp[, value-sum])` signature for one table, scoped to the
    portfolio when it carries `portfolio_id` (else tenant-wide — that only ever
    OVER-invalidates, never serves stale). Picks whatever change-signal the table actually
    has: `created_at` for append-only ledgers, `as_of` + `market_value_local` for broker
    positions overwritten in place on re-sync, `snap_date` for the NAV series. Schema-
    defensive so a model without a given column never raises."""
    scope = []
    if hasattr(model, "tenant_id"):
        scope.append(model.tenant_id == tenant_id)
    if hasattr(model, "portfolio_id"):
        scope.append(model.portfolio_id == portfolio_id)
    cols = [func.count(model.id)]
    for name in ("created_at", "as_of", "snap_date"):
        if hasattr(model, name):
            cols.append(func.max(getattr(model, name)))
            break
    if hasattr(model, "market_value_local"):  # broker positions: value can change in place
        cols.append(func.sum(model.market_value_local))
    row = session.execute(select(*cols).where(*scope)).one()
    return ":".join(str(x) for x in row)


def portfolio_fingerprint(session: Session, tenant_id, portfolio_id) -> str:
    """A cheap content hash of every input that can change a portfolio's valuation / NAV
    / ledger-derived history: the ledger (transactions), broker positions, accounts, the
    recorded NAV series, and broker-authoritative realized lots (``RealizedLot`` has no
    ``portfolio_id`` column, so this term over-invalidates tenant-wide on any of the
    tenant's portfolios — safe, never stale), plus the global price + FX cache high-water
    dates. Any mutation moves at least one term, so a cache key built on this invalidates
    precisely when the data changes. ~8 indexed aggregates; well under a millisecond."""
    parts = [
        _term(session, models.Transaction, tenant_id, portfolio_id),
        _term(session, models.Position, tenant_id, portfolio_id),
        _term(session, models.Account, tenant_id, portfolio_id),
        _term(session, models.AccountNavSnapshot, tenant_id, portfolio_id),
        _term(session, models.NavSnapshot, tenant_id, portfolio_id),
        _term(session, models.RealizedLot, tenant_id, portfolio_id),
    ]
    # Global reference data (not tenant-scoped): newest cached price + FX dates.
    latest_bar = session.execute(select(func.max(models.PriceBar.bar_date))).scalar()
    latest_fx = session.execute(select(func.max(models.FxRate.rate_date))).scalar()
    return "|".join(parts) + f"|pb:{latest_bar}|fx:{latest_fx}"


def cached(key: str, compute: Callable[[], T]) -> T:
    """Return `compute()`'s result for `key`, computed at most once per (key, content)
    across concurrent callers. The first caller for a fresh key computes while others
    block on that key's lock and then read the stored value. Include a
    `portfolio_fingerprint` in `key` so the entry self-invalidates on any data change."""
    now = time.monotonic()
    with _lock:
        hit = _entries.get(key)
        if hit is not None and now - hit[0] < _TTL_S:
            return hit[1]  # type: ignore[return-value]
        key_lock = _key_locks.setdefault(key, threading.Lock())

    with key_lock:
        # Re-check inside the per-key lock: a racing caller may have just populated it.
        with _lock:
            hit = _entries.get(key)
            if hit is not None and time.monotonic() - hit[0] < _TTL_S:
                return hit[1]  # type: ignore[return-value]
        value = compute()  # may raise — intentionally NOT cached (fail-loud, retry next call)
        with _lock:
            _entries[key] = (time.monotonic(), value)
            _evict_locked()
        return value


def _evict_locked() -> None:
    """Drop entries past the eviction-backstop TTL. Caller holds `_lock`."""
    cutoff = time.monotonic() - _TTL_S
    stale = [k for k, (ts, _v) in _entries.items() if ts < cutoff]
    for k in stale:
        _entries.pop(k, None)
        _key_locks.pop(k, None)


def clear() -> None:
    """Drop all cached entries — for tests, so one test's cache never bleeds into another."""
    with _lock:
        _entries.clear()
        _key_locks.clear()
