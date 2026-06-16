"""Demo / sample portfolio (metron-ops#42) — seeded, renders end-to-end, and is
READ-ONLY (the demo tenant refuses every mutating request)."""

from __future__ import annotations

import io

from api.db import models
from api.services import demo

DEMO_HEADERS = {"X-Tenant-Id": str(demo.DEMO_TENANT_ID)}


def test_seed_is_idempotent(db_session):
    assert demo.ensure_demo_seeded(db_session) is True
    assert demo.ensure_demo_seeded(db_session) is False
    portfolio = db_session.get(models.Portfolio, demo.DEMO_PORTFOLIO_ID)
    assert portfolio is not None
    assert portfolio.name == "Demo portfolio"


def test_demo_holdings_span_asset_classes(client, db_session):
    demo.ensure_demo_seeded(db_session)
    r = client.get(f"/portfolios/{demo.DEMO_PORTFOLIO_ID}/holdings", headers=DEMO_HEADERS)
    assert r.status_code == 200
    body = r.json()
    # The demo deliberately spans accounts + asset classes (equity / ETF / bond / cash)
    # so the tax-status (#46) and security-type (#47) groupings both showcase on it.
    tickers = {h["ticker"] for h in body}
    assert {"AAPL", "MSFT", "VOO", "912828YK0", "VMFXX"} <= tickers
    # Holdings value off the seeded frozen prices (no live refresh needed).
    assert all(h["market_value"] is not None for h in body)


def test_demo_has_two_tax_groups(client, db_session):
    demo.ensure_demo_seeded(db_session)
    r = client.get(f"/portfolios/{demo.DEMO_PORTFOLIO_ID}/accounts", headers=DEMO_HEADERS)
    assert r.status_code == 200
    treatments = {a["tax_treatment"] for a in r.json()}
    # A taxable (null → derived) + a tax-deferred account so the #46 grouping shows.
    assert "tax_deferred" in treatments


def test_demo_performance_has_metrics(client, db_session):
    demo.ensure_demo_seeded(db_session)
    r = client.get(f"/portfolios/{demo.DEMO_PORTFOLIO_ID}/performance", headers=DEMO_HEADERS)
    assert r.status_code == 200
    p = r.json()
    assert p["twr"] is not None
    assert p["annualized_twr"] is not None  # ≥30-day span → annualizes (per #44 guard)
    assert p["alpha"] is not None  # spy_close seeded → benchmark comparison


def test_demo_is_read_only_refresh(client, db_session):
    demo.ensure_demo_seeded(db_session)
    r = client.post(f"/portfolios/{demo.DEMO_PORTFOLIO_ID}/prices/refresh", headers=DEMO_HEADERS)
    assert r.status_code == 403
    assert "read-only" in r.json()["detail"].lower()


def test_demo_is_read_only_import(client, db_session):
    demo.ensure_demo_seeded(db_session)
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,TSLA,1,100,100,Demo Brokerage\n"
    r = client.post(
        f"/portfolios/{demo.DEMO_PORTFOLIO_ID}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=DEMO_HEADERS,
    )
    assert r.status_code == 403


def test_demo_is_read_only_patch(client, db_session):
    demo.ensure_demo_seeded(db_session)
    r = client.patch(
        f"/portfolios/{demo.DEMO_PORTFOLIO_ID}", json={"name": "Hijacked"}, headers=DEMO_HEADERS
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
