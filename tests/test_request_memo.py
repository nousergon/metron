"""Request-scoped read sharing does not change reported numbers.

The holdings page and glance call the ledger, the NAV series and the latest closes
several times per request. A request-scoped session computes each once. These tests
lock two things: the shared result equals a session that does not share, and a write
drops the memo so the next read sees the write.
"""

from __future__ import annotations

from datetime import date, timedelta

from api.db import models
from api.db.session import REQUEST_SCOPED
from api.services import analytics, performance


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


def test_column_load_keeps_estimate_stamps(db_session, session_factory):
    """Leaving the composition blob unread must not drop the estimate stamp the tiles show."""
    tenant_id, pid, _a1 = _book(db_session)
    from sqlalchemy import select

    row = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == pid).order_by(models.NavSnapshot.snap_date)
    ).first()
    assert row is not None
    row.estimated_inputs = [{"ticker": "ZZZ", "input": "price", "reason": "stale"}]
    db_session.commit()

    scoped = session_factory()
    scoped.info[REQUEST_SCOPED] = True
    try:
        summary = performance.performance(scoped, tenant_id, pid)
    finally:
        scoped.close()
    stamped = [p for p in summary.points if p.estimated_inputs]
    assert len(stamped) == 1
    assert stamped[0].estimated_inputs[0]["ticker"] == "ZZZ"
    # The composition blob is not what this series is for; the points still carry nav.
    assert summary.latest_nav == 130
