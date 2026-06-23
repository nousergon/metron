"""Regression: NAV reconstruction must NOT replay a snapshot-sourced account's
transaction/activity feed.

SnapTrade/Flex accounts populate BOTH ``positions`` (the authoritative current
holdings) AND ``transactions`` (activity history). ``analytics.holdings()`` excludes
their transactions from the current-holdings ledger via ``_snapshot_sourced_account_ids``
so shares + cost basis don't double-count. ``reconstruct_snapshots`` used a NARROWER
exclusion (only accounts with broker LOT rows), so a SnapTrade account — snapshot-sourced
but with no lots — had its (partial, sometimes ×100-on-bonds) activity feed replayed into
the historical NAV, inflating it (the >$1M reconstructed-NAV bug, same class as the
metron-ops#74 $4.5M incident through a path that fix didn't close).

The fix unifies the boundary: reconstruction now excludes the SAME
``_snapshot_sourced_account_ids`` set, so a no-lot snapshot account is carried
flat-backward at its current broker value, never replayed.
"""

from __future__ import annotations

from datetime import date

import pytest

from api.db import models
from api.services import performance
from portfolio_analytics.prices import ClosePoint


def _hist(symbols, start, end, *, source=None):
    # AAPL + SPY priced flat across the span (FNILX has no cached close → valued from the
    # broker snapshot / flat-backward carry, never a fabricated bar).
    flat = {
        "AAPL": [ClosePoint(start, 100.0), ClosePoint(end, 100.0)],
        "SPY": [ClosePoint(start, 480.0), ClosePoint(end, 500.0)],
    }
    return {s: flat[s] for s in symbols if s in flat}


def _seed_mixed_portfolio(session):
    """A realistic portfolio: a CSV (ledger-sourced) account giving the historical span,
    plus a SnapTrade (snapshot-sourced, no lots) account whose CURRENT position is 100 sh
    (broker MV $2,400, no price bar) but whose activity ledger holds a DUPLICATED buy
    (200 sh) — the partial/erroneous feed that, if replayed, inflates the NAV."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()

    csv = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD",
    )
    snap = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade",
        external_id="FID-1", institution="Fidelity", currency="USD",
    )
    aapl = models.Security(symbol="AAPL", currency="USD")
    fnilx = models.Security(symbol="FNILX", currency="USD")
    session.add_all([csv, snap, aapl, fnilx])
    session.flush()

    # CSV ledger: BUY 10 AAPL @ $100 → a correct, replayable position (the span anchor).
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=csv.id, security_id=aapl.id,
            txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 2), source_key="csv-buy",
        )
    )
    # SnapTrade activity: TWO buys of 100 sh (a duplicated/over-reported feed) → a naive
    # replay yields 200 sh.
    for i in range(2):
        session.add(
            models.Transaction(
                tenant_id=tenant.id, account_id=snap.id, security_id=fnilx.id,
                txn_type="BUY", quantity=100, price=20.0, amount=2000.0, currency="USD",
                trade_date=date(2024, 1, 2), source_key=f"snap-buy-{i}",
            )
        )
    # SnapTrade position snapshot — the TRUTH: 100 sh now worth $2,400 (no price bar exists).
    session.add(
        models.Position(
            tenant_id=tenant.id, account_id=snap.id, security_id=fnilx.id,
            quantity=100, avg_cost=20.0, currency="USD",
            market_price=24.0, market_value_local=2400.0, as_of=date(2024, 3, 20),
        )
    )
    session.commit()
    return tenant.id, pf.id


def test_snapshot_account_not_replayed_into_reconstructed_nav(client, db_session):
    tenant_id, pid = _seed_mixed_portfolio(db_session)
    n = performance.reconstruct_snapshots(
        db_session, tenant_id, pid, today=date(2024, 3, 20), source=_hist
    )
    assert n > 0

    p = client.get(f"/portfolios/{pid}/performance", headers={"X-Tenant-Id": str(tenant_id)}).json()
    navs = [pt["nav"] for pt in p["points"]]
    assert navs, "expected reconstructed snapshots"
    # AAPL 10 × $100 ($1,000, replayed from the CSV ledger) + FNILX carried flat at the
    # broker truth ($2,400). NOT $1,000 + 200×$24 ($4,800) = $5,800, which is what replaying
    # the SnapTrade activity would have produced.
    for nav in navs:
        assert nav == pytest.approx(3400.0), f"snapshot account was replayed → inflated NAV {nav}"
