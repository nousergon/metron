"""Request-scoped read sharing does not change reported numbers.

The holdings page and glance call the ledger, the NAV series and the latest closes
several times per request. A request-scoped session computes each once. These tests
lock two things: the shared result equals a session that does not share, and a write
drops the memo so the next read sees the write.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select, update

from api.db import models
from api.db.session import REQUEST_SCOPED
from api.services import analytics, compute_cache, performance


def _book(session):
    tenant = models.Tenant(name="memo")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    a1 = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="C1",
        name="A", currency="USD",
    )
    a2 = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="C2",
        name="B", currency="USD",
    )
    session.add_all([a1, a2])
    session.flush()
    sec = models.Security(symbol="ZZZ", name="ZZZ", currency="USD", asset_class="equity")
    session.add(sec)
    session.flush()
    start = date.today() - timedelta(days=40)
    session.add_all([
        models.Transaction(
            tenant_id=tenant.id, account_id=a1.id, security_id=sec.id, txn_type="BUY",
            quantity=2, price=10, amount=20, currency="USD", trade_date=start, source_key="b1",
        ),
        models.Transaction(
            tenant_id=tenant.id, account_id=a2.id, security_id=sec.id, txn_type="BUY",
            quantity=4, price=10, amount=40, currency="USD", trade_date=start, source_key="b2",
        ),
        models.PriceBar(security_id=sec.id, bar_date=start, close=12, currency="USD"),
        models.NavSnapshot(
            tenant_id=tenant.id, portfolio_id=pf.id, snap_date=start, nav=100, cost_basis=60,
            external_flow=0, spy_close=400,
            composition={"schema": 1, "legs": [{"ticker": "ZZZ", "qty": 6, "price": 10, "fx_rate": 1}]},
        ),
        models.NavSnapshot(
            tenant_id=tenant.id, portfolio_id=pf.id, snap_date=date.today(), nav=130, cost_basis=60,
            external_flow=0, spy_close=410,
            composition={"schema": 1, "legs": [{"ticker": "ZZZ", "qty": 6, "price": 12, "fx_rate": 1}]},
        ),
    ])
    session.commit()
    return tenant.id, pf.id, a1.id


def test_filtered_ledger_matches_the_sql_path(db_session, session_factory):
    tenant_id, pid, a1 = _book(db_session)
    plain = analytics.engine_transactions(db_session, tenant_id, pid, account_id=a1)
    scoped = session_factory()
    scoped.info[REQUEST_SCOPED] = True
    try:
        everything = analytics.engine_transactions(scoped, tenant_id, pid)
        one = analytics.engine_transactions(scoped, tenant_id, pid, account_id=a1)
    finally:
        scoped.close()
    assert len(everything) == 2
    assert [(t.quantity, t.amount) for t in one] == [(t.quantity, t.amount) for t in plain]
    assert len(one) == 1


def test_summary_from_shared_holdings_matches_its_own_valuation(db_session):
    tenant_id, pid, _a1 = _book(db_session)
    held = analytics.valued_holdings(db_session, tenant_id, pid)
    own = analytics.summary(db_session, tenant_id, pid)
    shared = analytics.summary(db_session, tenant_id, pid, held=held)
    assert shared.market_value == own.market_value
    assert shared.cash == own.cash
    assert shared.total_cost_basis == own.total_cost_basis
    assert shared.unrealized_gain == own.unrealized_gain
    assert shared.n_holdings == own.n_holdings


def test_repeated_performance_matches_and_a_write_drops_the_memo(db_session, session_factory, statements):
    tenant_id, pid, _a1 = _book(db_session)
    fresh = performance.performance(db_session, tenant_id, pid)

    scoped = session_factory()
    scoped.info[REQUEST_SCOPED] = True
    try:
        statements.clear()
        first = performance.performance(scoped, tenant_id, pid)
        nav_reads = sum("FROM nav_snapshots" in q for q in statements)
        second = performance.performance(scoped, tenant_id, pid)
        nav_reads_after = sum("FROM nav_snapshots" in q for q in statements)
        assert first.twr == fresh.twr
        assert first.latest_nav == fresh.latest_nav == second.latest_nav
        assert first.latest_cost_basis == fresh.latest_cost_basis
        assert nav_reads_after == nav_reads  # the second call did not re-read the series

        scoped.add(models.NavSnapshot(
            tenant_id=tenant_id, portfolio_id=pid, snap_date=date.today() + timedelta(days=1),
            nav=140, cost_basis=60, external_flow=10,
        ))
        scoped.flush()
        third = performance.performance(scoped, tenant_id, pid)
        assert third.n_snapshots == first.n_snapshots + 1
        assert third.latest_nav == 140
    finally:
        scoped.close()


def test_column_load_keeps_estimate_stamps(db_session, session_factory, statements):
    """Leaving the composition blob unread must not drop the estimate stamp the tiles show."""
    tenant_id, pid, _a1 = _book(db_session)
    row = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid).order_by(models.NavSnapshot.snap_date)
    ).first()
    assert row is not None
    row.estimated_inputs = [{"ticker": "ZZZ", "input": "price", "reason": "stale"}]
    db_session.commit()

    scoped = session_factory()
    scoped.info[REQUEST_SCOPED] = True
    try:
        statements.clear()
        summary = performance.performance(scoped, tenant_id, pid)
    finally:
        scoped.close()
    stamped = [p for p in summary.points if p.estimated_inputs]
    assert len(stamped) == 1
    assert stamped[0].estimated_inputs is not None
    assert stamped[0].estimated_inputs[0]["ticker"] == "ZZZ"
    # The composition blob is not what this series is for; the points still carry nav.
    assert summary.latest_nav == 130
    assert not any("composition" in sql for sql in statements)


def _scoped(session_factory):
    session = session_factory()
    session.info[REQUEST_SCOPED] = True
    return session


def test_two_tenants_keep_their_own_dates_and_currencies(db_session, session_factory):
    """A memo filled for one book must not answer another tenant, currency, or date window."""
    start = date.today() - timedelta(days=80)
    later = date.today() - timedelta(days=10)
    euro_start = date.today() - timedelta(days=20)
    euro_end = date.today() - timedelta(days=5)

    def _tenant(name: str, symbol: str, ccy: str, qty: float, snaps: list[tuple[date, float]]) -> tuple:
        tenant = models.Tenant(name=name)
        db_session.add(tenant)
        db_session.flush()
        pf = models.Portfolio(tenant_id=tenant.id, name=name, base_currency="USD")
        db_session.add(pf)
        db_session.flush()
        acct = models.Account(
            tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id=f"{name}-1",
            name=name, currency="USD",
        )
        db_session.add(acct)
        db_session.flush()
        sec = models.Security(symbol=symbol, name=symbol, currency=ccy, asset_class="equity")
        db_session.add(sec)
        db_session.flush()
        db_session.add(models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id, txn_type="BUY",
            quantity=qty, price=10, amount=10 * qty, currency=ccy, trade_date=snaps[0][0],
            source_key=f"{name}-buy",
        ))
        db_session.add(models.PriceBar(security_id=sec.id, bar_date=snaps[-1][0], close=10, currency=ccy))
        for when, nav in snaps:
            db_session.add(models.NavSnapshot(
                tenant_id=tenant.id, portfolio_id=pf.id, snap_date=when, nav=nav, cost_basis=60,
                external_flow=0, spy_close=400,
            ))
        db_session.commit()
        return tenant.id, pf.id

    usd_id, usd_pf = _tenant("usd", "ZZZ2", "USD", 2, [(start, 100.0), (later, 130.0)])
    eur_id, eur_pf = _tenant("eur", "YYY", "EUR", 3, [(euro_start, 200.0), (euro_end, 250.0)])
    db_session.add(models.FxRate(currency="EUR", base="USD", rate_date=euro_end, rate=2.0))
    db_session.commit()

    plain_usd = analytics.valued_holdings(db_session, usd_id, usd_pf)
    plain_eur = analytics.valued_holdings(db_session, eur_id, eur_pf)
    assert plain_usd[0].market_value == plain_usd[0].market_value_local  # USD rate is 1
    assert plain_eur[0].market_value == plain_eur[0].market_value_local * 2.0

    scoped = _scoped(session_factory)
    try:
        usd_tx = analytics.engine_transactions(scoped, usd_id, usd_pf)
        eur_tx = analytics.engine_transactions(scoped, eur_id, eur_pf)
        assert [t.currency for t in usd_tx] == ["USD"]
        assert [t.currency for t in eur_tx] == ["EUR"]
        usd_perf = performance.performance(scoped, usd_id, usd_pf)
        eur_perf = performance.performance(scoped, eur_id, eur_pf)
        assert [p.snap_date for p in usd_perf.points] == [start, later]
        assert [p.snap_date for p in eur_perf.points] == [euro_start, euro_end]
        assert usd_perf.latest_nav == 130
        assert eur_perf.latest_nav == 250
        held = analytics.valued_holdings(scoped, eur_id, eur_pf)
        assert held[0].market_value == plain_eur[0].market_value
    finally:
        scoped.close()


def test_a_raised_compute_is_not_stored(session_factory):
    scoped = _scoped(session_factory)
    calls = {"n": 0}

    def _boom() -> int:
        calls["n"] += 1
        raise RuntimeError("not stored")

    try:
        with pytest.raises(RuntimeError, match="not stored"):
            compute_cache.request_memo(scoped, ("boom",), _boom)
        assert compute_cache.request_memo(scoped, ("boom",), lambda: 7) == 7
        assert calls["n"] == 1
    finally:
        scoped.close()


def test_core_write_and_rollback_drop_the_memo(db_session, session_factory, statements):
    tenant_id, pid, _a1 = _book(db_session)
    scoped = _scoped(session_factory)
    try:
        statements.clear()
        first = performance.performance(scoped, tenant_id, pid)
        reads = sum("FROM nav_snapshots" in sql for sql in statements)
        row_id = scoped.scalars(
            select(models.NavSnapshot.id).where(
                models.NavSnapshot.portfolio_id == pid,
                models.NavSnapshot.snap_date == date.today(),
            )
        ).one()
        scoped.execute(update(models.NavSnapshot).where(models.NavSnapshot.id == row_id).values(nav=999))
        statements.clear()
        after_write = performance.performance(scoped, tenant_id, pid)
        assert sum("FROM nav_snapshots" in sql for sql in statements) >= 1
        assert after_write.latest_nav == 999
        scoped.rollback()
        restored = performance.performance(scoped, tenant_id, pid)
        assert restored.latest_nav == first.latest_nav
        assert restored.n_snapshots == first.n_snapshots
        assert reads >= 1
    finally:
        scoped.close()


def test_close_drops_the_memo_even_with_no_open_transaction(session_factory):
    scoped = _scoped(session_factory)
    try:
        assert compute_cache.request_memo(scoped, ("k",), lambda: 1) == 1
        assert scoped.get_transaction() is None
    finally:
        scoped.close()
    assert "metron.compute_cache.reads" not in scoped.info
    assert "metron.compute_cache.fingerprints" not in scoped.info
    scoped.info[REQUEST_SCOPED] = True
    try:
        assert compute_cache.request_memo(scoped, ("k",), lambda: 2) == 2
    finally:
        scoped.close()
