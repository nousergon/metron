"""The process-level compute cache: memoize within a content fingerprint, recompute
across one, and never cache an error (fail-loud)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import event

from api.db import models
from api.db.session import REQUEST_SCOPED
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

    db_session.add(
        models.RealizedLot(
            tenant_id=uuid.UUID(tenant), account_id=aid, ticker="AAPL",
            open_date=date(2024, 1, 1), close_date=date(2024, 2, 1),
            quantity=5, proceeds=600.0, cost_basis=500.0, currency="USD",
            source="ibkr_flex", lot_key="lot-1",
        )
    )
    db_session.commit()
    fp3 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    # RealizedLot has no portfolio_id column, so this term is tenant-wide — a stored
    # broker-authoritative lot (e.g. an IBKR Flex resync) must still invalidate the
    # fingerprint even though no Transaction/Position/Account row changed (metron-ops#198).
    assert fp3 != fp2


def _count_statements(session):
    seen: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        seen.append(statement)

    event.listen(session.get_bind(), "after_cursor_execute", _count)
    return seen, lambda: event.remove(session.get_bind(), "after_cursor_execute", _count)


def test_fingerprint_is_one_statement(db_session):
    tenant = str(uuid.uuid4())
    pid, _aid, _sid = _seed(db_session, tenant)
    db_session.commit()
    seen, stop = _count_statements(db_session)
    try:
        compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    finally:
        stop()
    assert len(seen) == 1


def test_request_scoped_session_memoizes_until_its_view_can_change(db_session):
    """A request session (``get_session`` marks it) computes the fingerprint once, however
    many cached reads sign their keys with it — and recomputes after its own write
    (flush / commit) or a rollback, so a key can never outlive the data it signed."""
    tenant = str(uuid.uuid4())
    pid, aid, sid = _seed(db_session, tenant)
    db_session.commit()
    db_session.info[REQUEST_SCOPED] = True
    seen, stop = _count_statements(db_session)
    try:
        fp0 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
        for _ in range(5):
            assert compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid) == fp0
        assert len(seen) == 1

        db_session.add(
            models.Transaction(
                tenant_id=uuid.UUID(tenant), account_id=aid, security_id=sid,
                txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
                trade_date=date(2024, 1, 2), source_key="b1",
            )
        )
        db_session.flush()  # the session's own write — memo dropped before commit
        fp1 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
        assert fp1 != fp0
        db_session.rollback()
        assert compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid) == fp0
    finally:
        stop()



def test_core_write_in_the_same_request_drops_the_memo(db_session):
    """A Core statement through ``session.execute`` (the price upsert path) writes with no
    ORM flush; the next fingerprint in that request must still see it."""
    from sqlalchemy import insert

    tenant = str(uuid.uuid4())
    pid, _aid, sid = _seed(db_session, tenant)
    db_session.commit()
    db_session.info[REQUEST_SCOPED] = True
    fp0 = compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    db_session.execute(
        insert(models.PriceBar).values(
            id=uuid.uuid4(), security_id=sid, bar_date=date(2031, 1, 2), close=1.0, currency="USD"
        )
    )
    assert compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid) != fp0
    db_session.rollback()


def test_unscoped_session_never_memoizes(db_session):
    """Maintenance jobs hold one session for a whole run while other sessions write; they
    never opt in, so every call recomputes, as before."""
    tenant = str(uuid.uuid4())
    pid, _aid, _sid = _seed(db_session, tenant)
    db_session.commit()
    seen, stop = _count_statements(db_session)
    try:
        for _ in range(3):
            compute_cache.portfolio_fingerprint(db_session, uuid.UUID(tenant), pid)
    finally:
        stop()
    assert len(seen) == 3
