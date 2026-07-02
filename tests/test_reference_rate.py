"""Reference Rate showcase — connector mapping, the consumer-side artifact contract,
and the seeded read-only live portfolio under the demo tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from api.db import models
from api.services import demo
from portfolio_analytics.ingestion import reference_connector
from portfolio_analytics.ingestion.base import SNAPSHOT_SOURCES

REFERENCE_HEADERS = {"X-Tenant-Id": str(demo.DEMO_TENANT_ID)}

# A sample artifact in the exact shape the producer (executor/reference_rate.py) writes
# — this fixture IS the consumer side of the cross-repo contract.
SAMPLE_ARTIFACT = {
    "schema_version": 1,
    "as_of": "2026-06-18",
    "generated_at": "2026-06-18T20:35:00+00:00",
    "label": "Reference Rate",
    "disclaimer": "Illustrative reference portfolio. Not investment advice; no representation is made as to performance.",
    "base_currency": "USD",
    "account": {"net_liquidation": 1_001_593.11},
    "positions": [
        {"ticker": "AMD", "shares": 192, "avg_cost": 130.5, "market_value": 103175.04, "sector": "Information Technology"},
        {"ticker": "SPY", "shares": 658, "avg_cost": 600.0, "market_value": 491354.92, "sector": "Broad Market"},
    ],
    "nav_history": [
        {"date": "2026-06-16", "nav": 999_000.0, "spy_close": 744.0},
        {"date": "2026-06-17", "nav": 1_000_500.0, "spy_close": 745.5},
        {"date": "2026-06-18", "nav": 1_001_593.11, "spy_close": 746.74},
    ],
}


def _reader():
    return dict(SAMPLE_ARTIFACT)


# ── Connector mapping ────────────────────────────────────────────────────────


def test_reference_is_registered_snapshot_source():
    assert "reference" in SNAPSHOT_SOURCES


def test_artifact_to_snapshot_maps_positions_and_account():
    snap = reference_connector.artifact_to_snapshot(SAMPLE_ARTIFACT)
    assert snap.source == "reference"
    assert {h.security_id for h in snap.holdings} == {"EQ:AMD:USD", "EQ:SPY:USD"}
    assert {s.ticker for s in snap.securities} == {"AMD", "SPY"}
    amd = next(h for h in snap.holdings if h.security_id == "EQ:AMD:USD")
    assert amd.quantity == 192
    assert amd.avg_cost == 130.5
    assert amd.market_value_local == 103175.04
    # One account; cash is the reconciling plug nav − Σ market_value.
    assert len(snap.accounts) == 1
    acct = snap.accounts[0]
    assert acct.nav_usd == 1_001_593.11
    assert round(acct.cash_usd, 2) == round(1_001_593.11 - (103175.04 + 491354.92), 2)
    # Snapshot source — no activity ledger.
    assert snap.activities == []


def test_artifact_to_snapshot_drops_zero_share_rows():
    art = dict(SAMPLE_ARTIFACT)
    art["positions"] = [*SAMPLE_ARTIFACT["positions"], {"ticker": "OLD", "shares": 0, "avg_cost": 1, "market_value": 0, "sector": "x"}]
    snap = reference_connector.artifact_to_snapshot(art)
    assert "EQ:OLD:USD" not in {h.security_id for h in snap.holdings}


def test_connector_failsoft_on_missing_artifact():
    snap = reference_connector.ReferenceRateConnector(reader=lambda: None).sync()
    assert snap.error is not None
    assert snap.holdings == []


def test_connector_failsoft_on_reader_raise():
    def _boom():
        raise RuntimeError("s3 down")

    snap = reference_connector.ReferenceRateConnector(reader=_boom).sync()
    assert snap.error and "s3 down" in snap.error


# ── Seeded showcase under the demo tenant ────────────────────────────────────


def test_ensure_reference_seeded_idempotent(db_session):
    assert demo.ensure_reference_seeded(db_session) is True
    assert demo.ensure_reference_seeded(db_session) is False
    p = db_session.get(models.Portfolio, demo.REFERENCE_PORTFOLIO_ID)
    assert p is not None and p.name == "Reference Rate"
    assert p.tenant_id == demo.DEMO_TENANT_ID  # under the read-only demo tenant


def test_sync_persists_holdings_sectors_and_nav(db_session):
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True

    acct_ids = db_session.scalars(
        select(models.Account.id).where(models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    positions = db_session.scalars(
        select(models.Position).where(models.Position.account_id.in_(acct_ids))
    ).all()
    assert len(positions) == 2

    amd = db_session.scalar(select(models.Security).where(models.Security.symbol == "AMD"))
    assert amd.sector == "Information Technology"

    navs = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    assert len(navs) == 3
    assert all(n.external_flow == 0.0 for n in navs)
    assert {str(n.snap_date) for n in navs} == {"2026-06-16", "2026-06-17", "2026-06-18"}


def test_sync_is_idempotent(db_session):
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True
    # Positions replaced (not doubled), NAV history upserted by snap_date (not duplicated).
    accts = db_session.scalars(
        select(models.Account).where(models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    assert len(accts) == 1
    navs = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    assert len(navs) == 3


def test_sync_failsoft_keeps_last_good(db_session):
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True
    # A subsequent failed sync (no artifact) must not blank the showcase.
    assert demo.sync_reference_holdings(db_session, reader=lambda: None) is False
    accts = db_session.scalars(
        select(models.Account).where(models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    assert len(accts) == 1


def test_holdings_endpoint_serves_reference(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=REFERENCE_HEADERS)
    assert r.status_code == 200
    assert {h["ticker"] for h in r.json()} == {"AMD", "SPY"}


def test_reference_portfolio_is_read_only(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    # The reference portfolio lives under the demo tenant → the read-only HTTP guard
    # refuses any mutating request to it.
    r = client.post(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/import/csv",
        headers=REFERENCE_HEADERS,
        files={"file": ("t.csv", b"date,type,symbol,quantity,price,amount,account\n", "text/csv")},
    )
    assert r.status_code == 403


# ── Cross-tenant visibility (metron-ops: "Reference Rate on every dashboard") ───────────


def _real_headers() -> dict[str, str]:
    return {"X-Tenant-Id": str(uuid.uuid4())}


def test_reference_portfolio_visible_to_real_tenant_list(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    real = _real_headers()
    own = client.post("/portfolios", json={"name": "My Portfolio"}, headers=real).json()
    ids = {p["id"] for p in client.get("/portfolios", headers=real).json()}
    assert own["id"] in ids
    assert str(demo.REFERENCE_PORTFOLIO_ID) in ids


def test_reference_portfolio_not_duplicated_for_demo_tenant(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    ids = [p["id"] for p in client.get("/portfolios", headers=REFERENCE_HEADERS).json()]
    assert ids.count(str(demo.REFERENCE_PORTFOLIO_ID)) == 1


def test_reference_portfolio_readable_by_real_tenant(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=_real_headers())
    assert r.status_code == 200
    assert {h["ticker"] for h in r.json()} == {"AMD", "SPY"}


def test_reference_portfolio_still_read_only_for_real_tenant(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    real = _real_headers()
    # A real tenant's OWN header never equals DEMO_TENANT_ID — this is the case the
    # tenant-header check alone can't catch; the middleware's path-based check must.
    r = client.post(
        f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/import/csv",
        headers=real,
        files={"file": ("t.csv", b"date,type,symbol,quantity,price,amount,account\n", "text/csv")},
    )
    assert r.status_code == 403

    r = client.patch(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}", json={"name": "Hijacked"}, headers=real)
    assert r.status_code == 403

    account_id = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/accounts/excluded", headers=real)
    assert account_id.status_code == 200  # reads still work — sanity check on the same portfolio
    acct_ids = db_session.scalars(
        select(models.Account.id).where(models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    r = client.delete(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/accounts/{acct_ids[0]}", headers=real)
    assert r.status_code == 403


def test_reference_portfolio_absent_from_list_when_unseeded(client):
    # No sync ran — the showcase hasn't been seeded — a real tenant's list must not 500
    # and must simply omit it.
    real = _real_headers()
    r = client.get("/portfolios", headers=real)
    assert r.status_code == 200
    assert str(demo.REFERENCE_PORTFOLIO_ID) not in {p["id"] for p in r.json()}
