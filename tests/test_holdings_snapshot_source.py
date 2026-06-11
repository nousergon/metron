"""Regression: a snapshot-sourced account (Flex/SnapTrade) must contribute its CURRENT
holdings from the ``positions`` snapshot only — NOT from BOTH ``positions`` and the
``transactions`` (activity) ledger that SnapTrade/Flex also populate.

The live bug (2026-06-11): Fidelity-via-SnapTrade accounts carried both a full activity
history and a position snapshot for funds like FNILX. ``holdings()`` unioned the two,
double-counting shares + cost basis. Because the Fidelity ZERO funds have no cached price
bar, market value came from the broker snapshot (single) while cost was doubled — a
~$139K phantom unrealized LOSS on accounts that were actually up ~$24K.
"""

from __future__ import annotations

import uuid
from datetime import date

from api.db import models
from api.services import analytics


def _seed_snapshot_account_with_activities(session):
    """One SnapTrade-shaped account: a BUY transaction AND a position snapshot for the
    SAME ticker — the on-disk shape that triggered the double-count."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade",
        external_id="FID-1", institution="Fidelity", currency="USD",
    )
    sec = models.Security(symbol="FNILX", currency="USD")
    session.add_all([acct, sec])
    session.flush()
    # Activity ledger (SnapTrade also writes these): bought 100 sh @ $20 = $2,000 cost.
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type="BUY", quantity=100, price=20.0, amount=2000.0, currency="USD",
            trade_date=date(2024, 1, 1), source_key="buy-1",
        )
    )
    # Broker position snapshot for the SAME 100 sh, now worth $2,400 (no price bar exists,
    # so valuation falls back to this broker market value).
    session.add(
        models.Position(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            quantity=100, avg_cost=20.0, currency="USD",
            market_price=24.0, market_value_local=2400.0, as_of=date(2024, 6, 3),
        )
    )
    session.commit()
    return tenant.id, pf.id


def test_snapshot_account_not_double_counted(db_session):
    tenant_id, pid = _seed_snapshot_account_with_activities(db_session)
    held = analytics.holdings(db_session, tenant_id, pid)
    assert len(held) == 1
    h = held[0]
    assert h.ticker == "FNILX"
    # Single-counted: 100 shares, $2,000 cost — NOT 200 / $4,000.
    assert h.quantity == 100
    assert h.cost_basis == 2000.0
    # Broker market value stays the single snapshot value (per share, qty-weighted).
    assert h.broker_market_price == 24.0


def test_snapshot_account_valuation_is_a_gain_not_phantom_loss(db_session):
    """End-to-end: no cached price bar → broker-MV fallback. With the double-count the
    cost basis was 2x the market value (phantom loss); the fix restores the real gain."""
    tenant_id, pid = _seed_snapshot_account_with_activities(db_session)
    valued = analytics.valued_holdings(db_session, tenant_id, pid)
    h = valued[0]
    assert h.market_value_local == 2400.0  # 100 sh × $24 broker price
    assert h.unrealized_gain == 400.0      # 2400 − 2000, a gain (was −1600 when doubled)


def test_csv_only_account_still_ledger_sourced(db_session):
    """An account with NO position snapshot (CSV/OFX) must still derive holdings from the
    ledger — the fix must not blank out ledger-only accounts."""
    tenant = models.Tenant(name="t2")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv",
        external_id="CSV-1", currency="USD",
    )
    sec = models.Security(symbol="AAPL", currency="USD")
    db_session.add_all([acct, sec])
    db_session.flush()
    db_session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 1), source_key="buy-csv-1",
        )
    )
    db_session.commit()
    held = analytics.holdings(db_session, tenant.id, pf.id)
    assert len(held) == 1
    assert held[0].ticker == "AAPL"
    assert held[0].quantity == 10
    assert held[0].cost_basis == 1000.0
