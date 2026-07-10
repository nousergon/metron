"""Reference Rate's frozen sample sleeve (metron-ops#42) — the no-auth `/demo` entry
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


def test_demo_holdings_span_asset_classes(client, db_session):
    demo.ensure_reference_seeded(db_session)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=DEMO_HEADERS)
    assert r.status_code == 200
    body = r.json()
    # The sample sleeve deliberately spans accounts + asset classes (equity / ETF / bond /
    # cash) so the tax-status (#46) and security-type (#47) groupings both showcase on it.
    tickers = {h["ticker"] for h in body}
    assert {"AAPL", "MSFT", "VOO", "912828YK0", "VMFXX"} <= tickers
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
