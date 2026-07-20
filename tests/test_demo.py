"""Showcase Portfolio's frozen sample sleeve (metron-ops#42) — the no-auth `/demo` entry
point's asset-class/tax breadth fixture, folded into REFERENCE_PORTFOLIO_ID. Seeded,
renders end-to-end, and is READ-ONLY (the demo tenant refuses every mutating request)."""

from __future__ import annotations

import io

from sqlalchemy import select

from api.db import models
from api.services import demo

DEMO_HEADERS = {"X-Tenant-Id": str(demo.DEMO_TENANT_ID)}


def test_sample_sleeve_seed_is_idempotent(db_session):
    demo.ensure_reference_seeded(db_session)
    demo.ensure_reference_seeded(db_session)  # re-seeding must not duplicate accounts
    accounts = db_session.scalars(
        select(models.Account).where(
            models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
            models.Account.broker == demo._SAMPLE_SLEEVE_SOURCE,
        )
    ).all()
    assert len(accounts) == 2  # Sample Brokerage + Sample IRA, exactly once


def test_sample_sleeve_reconciles_retired_symbols(db_session):
    """metron-ops-I201: an already-deployed instance seeded under an older fixture
    version (that carried AAPL/MSFT, retired by metron-PR230 on 2026-07-13) must
    self-heal on the next ``ensure_reference_seeded`` call — the prior
    ``_ensure_sample_sleeve_seeded`` only checked "has the sleeve ever been seeded"
    and skipped forever once true, so a fixture edit never reached already-persisted
    data. This reproduces that exact scenario against the real reconcile path."""
    from api.services import persistence
    from portfolio_analytics.broker_io.csv_import import parse_transactions_csv

    # Simulate the pre-PR230 seed: the old fixture carried AAPL/MSFT alongside VOO,
    # under the same account labels the current fixture still uses.
    old_csv = (
        "date,type,symbol,quantity,price,amount,account\n"
        "2024-01-08,BUY,AAPL,40,185,7400,Sample Brokerage\n"
        "2024-01-08,BUY,VOO,15,440,6600,Sample Brokerage\n"
        "2024-01-16,BUY,MSFT,20,390,7800,Sample IRA\n"
    )
    result = parse_transactions_csv(old_csv, source=demo._SAMPLE_SLEEVE_SOURCE)
    persistence.persist_snapshot(
        db_session,
        tenant_id=demo.DEMO_TENANT_ID,
        portfolio_id=demo.REFERENCE_PORTFOLIO_ID,
        snapshot=result.snapshot,
    )
    aapl = db_session.scalars(select(models.Security).where(models.Security.symbol == "AAPL")).one()
    db_session.add(
        models.PriceBar(
            security_id=aapl.id, bar_date=demo._SAMPLE_SLEEVE_PRICE_AS_OF, close=210.0, currency="USD"
        )
    )
    db_session.commit()

    # Run the real startup path against the CURRENT (trimmed) fixture.
    demo.ensure_reference_seeded(db_session)

    tickers = set(
        db_session.scalars(
            select(models.Security.symbol)
            .join(models.Transaction, models.Transaction.security_id == models.Security.id)
            .join(models.Account, models.Transaction.account_id == models.Account.id)
            .where(
                models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
                models.Account.broker == demo._SAMPLE_SLEEVE_SOURCE,
            )
        ).all()
    )
    assert "AAPL" not in tickers and "MSFT" not in tickers
    assert {"VOO", "912828YK0", "VMFXX"} <= tickers

    # This module's own frozen price bar for the retired symbol is pruned too.
    remaining_bar = db_session.scalars(
        select(models.PriceBar).where(
            models.PriceBar.security_id == aapl.id,
            models.PriceBar.bar_date == demo._SAMPLE_SLEEVE_PRICE_AS_OF,
        )
    ).first()
    assert remaining_bar is None

    # NAV/holdings totals recompute off the reconciled sleeve, not the stale one.
    value, cost_basis = demo._sample_sleeve_totals(db_session)
    assert (value, cost_basis) == (14300.0, 13500.0)


def test_demo_holdings_span_asset_classes(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=DEMO_HEADERS)
    assert r.status_code == 200
    body = r.json()
    # The sample sleeve deliberately spans accounts + NON-EQUITY asset classes (ETF / bond /
    # cash) so the tax-status (#46) and security-type (#47) groupings both showcase on it —
    # it must never carry individual-stock equity of its own (that would inflate the
    # Showcase Portfolio's equity count beyond what Crucible's live sleeve actually holds).
    tickers = {h["ticker"] for h in body}
    assert {"VOO", "912828YK0", "VMFXX"} <= tickers
    assert "AAPL" not in tickers and "MSFT" not in tickers
    # Holdings value off the seeded frozen prices (no live refresh needed).
    assert all(h["market_value"] is not None for h in body)


def test_demo_has_two_tax_groups(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/accounts", headers=DEMO_HEADERS)
    assert r.status_code == 200
    treatments = {a["tax_treatment"] for a in r.json()}
    # A taxable (null -> derived) + a tax-deferred account so the #46 grouping shows.
    assert "tax_deferred" in treatments


def test_demo_is_read_only_refresh(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.post(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/prices/refresh", headers=DEMO_HEADERS)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_demo_is_read_only_import(client, db_session):
    demo.ensure_reference_seeded(db_session)
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,TSLA,1,100,100,Sample Brokerage\n"
    r = client.post(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=DEMO_HEADERS,
    )
    assert r.status_code == 403


def test_demo_is_read_only_patch(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.patch(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}", json={"name": "Hijacked"}, headers=DEMO_HEADERS
    )
    assert r.status_code == 403


def test_non_demo_tenant_still_writable(client):
    """The read-only guard is demo-only — a normal tenant's POST is unaffected."""
    import uuid

    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "Real"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,AAPL,1,100,100,Brokerage\n"
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200
