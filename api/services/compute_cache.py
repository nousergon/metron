"""Cross-process single-flight cache for expensive read computations, backed by
``diskcache`` so multiple gunicorn uvicorn workers share the same cache store.

A dashboard page fans out into several parallel API requests that each recompute the
same costly per-portfolio aggregates (NAV reconstruction, valued holdings). This caches
those results keyed by a **content fingerprint** of the portfolio's mutable state, so:

- a cache entry is valid for *exactly* as long as the underlying data is unchanged — a
  fingerprint mismatch (after any import / price refresh / account change) misses and
  recomputes, so there is **no staleness window** (unlike a pure TTL cache); and
- concurrent callers across multiple workers share a single computation via a per-key
  cross-process lock (single-flight via ``diskcache.Lock``) — no thundering-herd
  recompute when six requests land at once across six workers.

The TTL here is only a disk-eviction backstop (bound the number of retained
fingerprints), NOT a correctness mechanism. The ``diskcache.Cache`` backend uses SQLite
with file-level locking so workers across processes share the same cache store and
per-key lock. Errors are never cached — a failed compute raises to the caller
(fail-loud) and leaves the slot empty so the next request retries.

Two environment variables tune the location:
  ``METRON_COMPUTE_CACHE_DIR``  — path to the diskcache SQLite directory
                                  (default: ``$TMPDIR/metron-compute-cache``)
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from typing import TypeVar

import diskcache as dc
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from api.db import models
from api.db.session import REQUEST_SCOPED

T = TypeVar("T")

_TTL_S = 300.0  # disk-eviction backstop only; correctness comes from the fingerprint key
_CACHE_DIR = os.environ.get(
    "METRON_COMPUTE_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), "metron-compute-cache"),
)
_cache = dc.Cache(_CACHE_DIR)
_MISS = object()  # sentinel so None-valued entries are cacheable


#: Every table whose content can change a portfolio's valuation / NAV / ledger-derived
#: history. ``RealizedLot`` has no ``portfolio_id`` column,
#: so its term is tenant-wide (over-invalidates across the tenant's portfolios — safe, never
#: stale).
_FINGERPRINT_MODELS = (
    models.Transaction,
    models.Position,
    models.Account,
    models.AccountNavSnapshot,
    models.NavSnapshot,
    models.RealizedLot,
)

#: ``Session.info`` key under which a request-scoped session memoizes its fingerprints.
_MEMO_KEY = "metron.compute_cache.fingerprints"


def _term_columns(model, tenant_id, portfolio_id) -> list:
    """A `(count, freshest-timestamp[, value-sum])` signature for one table, scoped to the
    portfolio when it carries `portfolio_id` (else tenant-wide — that only ever
    OVER-invalidates, never serves stale), as scalar subqueries so every table's signature
    rides ONE round trip. Picks whatever change-signal the table actually has: `created_at`
    for append-only ledgers, `as_of` + `market_value_local` for broker positions overwritten
    in place on re-sync, `snap_date` for the NAV series. Schema-defensive so a model without
    a given column never raises."""
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
    return [select(col).where(*scope).scalar_subquery() for col in cols]


def _compute_fingerprint(session: Session, tenant_id, portfolio_id) -> str:
    groups = [_term_columns(model, tenant_id, portfolio_id) for model in _FINGERPRINT_MODELS]
    # Global reference data (not tenant-scoped): newest cached price + FX dates.
    latest_bar = select(func.max(models.PriceBar.bar_date)).scalar_subquery()
    latest_fx = select(func.max(models.FxRate.rate_date)).scalar_subquery()
    row = session.execute(select(*[c for g in groups for c in g], latest_bar, latest_fx)).one()
    values = list(row)
    parts = []
    for g in groups:
        parts.append(":".join(str(x) for x in values[: len(g)]))
        values = values[len(g):]
    return "|".join(parts) + f"|pb:{values[0]}|fx:{values[1]}"


def portfolio_fingerprint(session: Session, tenant_id, portfolio_id) -> str:
    """A cheap content hash of every input that can change a portfolio's valuation / NAV
    / ledger-derived history: the ledger (transactions), broker positions, accounts, the
    recorded NAV series, and broker-authoritative realized lots, plus the global price + FX
    cache high-water dates. Any mutation moves at least one term, so a cache key built on
    this invalidates precisely when the data changes.

    **One statement, and at most one per request.** Every ``compute_cache``-wrapped read
    (``holdings`` / ``realized`` / ``transactions`` / ``income`` / the account performance
    series) signs its key with this, and one page request makes 5-15 such calls — the
    per-account paths once per account. It used to be 8 separate aggregate queries per
    call, so on a warm Holdings landing page the fingerprint alone was 232 of the 355
    statements sent to Neon, each one a network round trip (production-shaped benchmark,
    2026-09-28; ``tests/test_landing_page_statement_budget.py`` pins the fix). Now the terms ride one ``SELECT`` of
    scalar subqueries, and a REQUEST-scoped session (``api.db.session.get_session`` marks
    it) memoizes the result until that session next flushes, commits or rolls back — the
    three points at which its own view of the data can change. Sessions that did not opt
    in (maintenance jobs, ad-hoc scripts) recompute on every call, exactly as before."""
    memo = session.info.get(_MEMO_KEY) if session.info.get(REQUEST_SCOPED) else None
    key = (str(tenant_id), str(portfolio_id))
    if memo is not None and key in memo:
        return memo[key]
    fp = _compute_fingerprint(session, tenant_id, portfolio_id)
    if session.info.get(REQUEST_SCOPED):
        session.info.setdefault(_MEMO_KEY, {})[key] = fp
    return fp


def _forget_fingerprints(session: Session, *_args) -> None:
    session.info.pop(_MEMO_KEY, None)


# The memo describes what the session could see when it was taken; anything that can change
# that view — its own writes (flush), a new transaction snapshot (commit / rollback) — drops it.
for _evt in ("after_flush", "after_commit", "after_rollback", "after_soft_rollback"):
    event.listen(Session, _evt, _forget_fingerprints)


def cached(key: str, compute: Callable[[], T]) -> T:
    """Return ``compute()``'s result for ``key``, computed at most once per (key, content)
    across all workers. The first caller for a fresh key computes while others
    block on that key's cross-process lock (``diskcache.Lock``) and then read the
    stored value. Include a ``portfolio_fingerprint`` in ``key`` so the entry
    self-invalidates on any data change."""
    entry = _cache.get(key, _MISS)
    if entry is not _MISS:
        return entry  # type: ignore[return-value]

    lock = dc.Lock(_cache, f"lck:{key}")
    with lock:
        # Re-check inside the cross-process lock: a racing worker may have just populated it.
        entry = _cache.get(key, _MISS)
        if entry is not _MISS:
            return entry  # type: ignore[return-value]
        value = compute()  # may raise — intentionally NOT cached (fail-loud, retry next call)
        _cache.set(key, value, expire=_TTL_S)
        return value


def clear() -> None:
    """Drop all cached entries — for tests, so one test's cache never bleeds into another."""
    _cache.clear()
