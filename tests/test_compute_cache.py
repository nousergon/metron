"""The process-level compute cache: memoize within a content fingerprint, recompute
across one, and never cache an error (fail-loud)."""

from __future__ import annotations

import uuid
from datetime import date

from api.db import models
from api.services import compute_cache


def test_cached_computes_once_per_key():
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return calls["n"]

    assert compute_cache.cached("k1", compute) == 1
    assert compute_cache.cached("k1", compute) == 1  # memoized — no recompute
    assert calls["n"] == 1
    assert compute_cache.cached("k2", compute) == 2  # different key recomputes
    assert calls["n"] == 2


def test_error_is_not_cached():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("nope")

    for _ in range(2):
        try:
            compute_cache.cached("err", boom)
        except ValueError:
            pass
    assert calls["n"] == 2  # each call retried — failure left the slot empty


def _seed(session, tenant):
    pid = uuid.uuid4()
    acct = models.Account(
        tenant_id=uuid.UUID(tenant), portfolio_id=pid, broker="csv", external_id="A1", name="A1"
    )
    sec = models.Security(symbol="AAPL", currency="USD")
    session.add_all([acct, sec])
    session.flush()
    return pid, acct.id, sec.id


def test_fingerprint_changes_on_mutation(db_session):
    tenant = str(uuid.uuid4())
    pid, aid, sid = _seed(db_session, tenant)
    db_session.commit()
    fp0 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    assert compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid) == fp0  # stable

    db_session.add(
        models.Transaction(
            tenant_id=uuid.UUID(tenant), account_id=aid, security_id=sid,
            txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 2), source_key="b1",
        )
    )
    db_session.commit()
    fp1 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    assert fp1 != fp0  # a new transaction must invalidate the cache key

    db_session.add(models.PriceBar(security_id=sid, bar_date=date(2024, 1, 3), close=120.0, currency="USD"))
    db_session.commit()
    fp2 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    assert fp2 != fp1  # a fresh price bar must invalidate too
