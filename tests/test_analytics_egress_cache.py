"""Regression tests for the metron-ops#198 Neon-egress fix: ``holdings()``/``realized()``/
``transactions()``/``income()`` are now wrapped in the same ``compute_cache`` fingerprint
cache ``account_performance_series`` already used, so a full ledger-replay/query no longer
runs on every call — only when the portfolio's underlying data actually changed.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from api.db import models
from api.services import analytics, compute_cache


def _seed_portfolio(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="A1", name="A1", currency="USD",
    )
    sec = models.Security(symbol="AAPL", currency="USD")
    session.add_all([acct, sec])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 2), source_key="b1",
        )
    )
    session.commit()
    return tenant.id, pf.id, acct.id, sec.id


def test_holdings_is_memoized_until_data_changes(db_session):
    tenant_id, pid, acct_id, sec_id = _seed_portfolio(db_session)

    with mock.patch("api.services.analytics._holdings", wraps=analytics._holdings) as spy:
        first = analytics.holdings(db_session, tenant_id, pid)
        second = analytics.holdings(db_session, tenant_id, pid)
        assert spy.call_count == 1  # second call served from cache, no re-query
    assert first == second

    # A new transaction changes the portfolio's content fingerprint → cache miss.
    db_session.add(
        models.Transaction(
            tenant_id=tenant_id, account_id=acct_id, security_id=sec_id,
            txn_type="BUY", quantity=5, price=110.0, amount=550.0, currency="USD",
            trade_date=date(2024, 2, 1), source_key="b2",
        )
    )
    db_session.commit()
    with mock.patch("api.services.analytics._holdings", wraps=analytics._holdings) as spy:
        third = analytics.holdings(db_session, tenant_id, pid)
        assert spy.call_count == 1
    assert third[0].quantity == 15  # reflects the new transaction, not a stale cache hit


def test_holdings_returns_independent_copies(db_session):
    """A caller (e.g. valued_holdings) mutates the Holding rows it gets back in place —
    that must never corrupt the shared cache entry for the next caller."""
    tenant_id, pid, _acct_id, _sec_id = _seed_portfolio(db_session)

    first = analytics.holdings(db_session, tenant_id, pid)
    first[0].security_type = "mutated-by-caller"
    first[0].quantity = 99999.0

    second = analytics.holdings(db_session, tenant_id, pid)
    assert second[0].quantity == 10  # unaffected by the first caller's in-place mutation
    assert second[0].security_type != "mutated-by-caller"
    assert second is not first
    assert second[0] is not first[0]


def test_transactions_is_memoized_until_data_changes(db_session):
    tenant_id, pid, acct_id, sec_id = _seed_portfolio(db_session)

    with mock.patch("api.services.analytics._transactions", wraps=analytics._transactions) as spy:
        analytics.transactions(db_session, tenant_id, pid)
        analytics.transactions(db_session, tenant_id, pid)
        assert spy.call_count == 1

    db_session.add(
        models.Transaction(
            tenant_id=tenant_id, account_id=acct_id, security_id=sec_id,
            txn_type="SELL", quantity=1, price=120.0, amount=120.0, currency="USD",
            trade_date=date(2024, 3, 1), source_key="s1",
        )
    )
    db_session.commit()
    with mock.patch("api.services.analytics._transactions", wraps=analytics._transactions) as spy:
        rows = analytics.transactions(db_session, tenant_id, pid)
        assert spy.call_count == 1
    assert len(rows) == 2


def test_realized_cache_invalidates_on_new_stored_lot(db_session):
    """RealizedLot (broker-authoritative closed lots, e.g. IBKR Flex) has no portfolio_id
    column, so the fingerprint scopes it tenant-wide — a stored lot for this tenant must
    still invalidate `realized()`'s cache even though no Transaction/Position row changed."""
    tenant_id, pid, acct_id, _sec_id = _seed_portfolio(db_session)

    fp0 = compute_cache.portfolio_fingerprint(db_session, tenant_id, pid)
    before = analytics.realized(db_session, tenant_id, pid)
    assert before == []

    db_session.add(
        models.RealizedLot(
            tenant_id=tenant_id, account_id=acct_id, ticker="AAPL",
            open_date=date(2023, 1, 1), close_date=date(2024, 1, 1),
            quantity=5, proceeds=600.0, cost_basis=500.0, currency="USD",
            source="ibkr_flex", lot_key="lot-1",
        )
    )
    db_session.commit()
    fp1 = compute_cache.portfolio_fingerprint(db_session, tenant_id, pid)
    assert fp1 != fp0

    after = analytics.realized(db_session, tenant_id, pid)
    assert len(after) == 1
    assert after[0].ticker == "AAPL"


def test_income_is_memoized_until_data_changes(db_session):
    """income() is the biggest remaining gap found post-#198: it's on totals()'s hot
    path (the Overview/home-page summary) but re-read _portfolio_rows directly instead
    of going through a cached wrapper."""
    tenant_id, pid, acct_id, sec_id = _seed_portfolio(db_session)
    db_session.add(
        models.Transaction(
            tenant_id=tenant_id, account_id=acct_id, security_id=sec_id,
            txn_type="DIVIDEND", quantity=0, price=0, amount=50.0, currency="USD",
            trade_date=date(2024, 6, 1), source_key="div1",
        )
    )
    db_session.commit()

    with mock.patch("api.services.analytics._income", wraps=analytics._income) as spy:
        first = analytics.income(db_session, tenant_id, pid)
        second = analytics.income(db_session, tenant_id, pid)
        assert spy.call_count == 1  # second call served from cache, no re-query
    assert first == second
    assert first[0].dividends == 50.0

    db_session.add(
        models.Transaction(
            tenant_id=tenant_id, account_id=acct_id, security_id=sec_id,
            txn_type="DIVIDEND", quantity=0, price=0, amount=25.0, currency="USD",
            trade_date=date(2024, 7, 1), source_key="div2",
        )
    )
    db_session.commit()
    with mock.patch("api.services.analytics._income", wraps=analytics._income) as spy:
        third = analytics.income(db_session, tenant_id, pid)
        assert spy.call_count == 1
    assert third[0].dividends == 75.0  # reflects the new dividend, not a stale cache hit
