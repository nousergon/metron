"""Reference Rate showcase — connector mapping, the consumer-side artifact contract,
and the seeded read-only live portfolio under the demo tenant."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from api.db import models
from api.maintenance import daily_refresh
from api.services import demo
from portfolio_analytics.ingestion import reference_connector
from portfolio_analytics.ingestion.base import SNAPSHOT_SOURCES
from portfolio_analytics.prices import ClosePoint

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


def _reference_pref(db_session) -> models.InvestorPreferences | None:
    return db_session.scalars(
        select(models.InvestorPreferences).where(
            models.InvestorPreferences.tenant_id == demo.DEMO_TENANT_ID,
            models.InvestorPreferences.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
        )
    ).first()


def test_ensure_reference_seeded_defaults_intraday_on(db_session):
    # The showcase's own read-only lockout (_demo_read_only) blocks the normal
    # PUT .../preferences path a real user would use to flip this toggle — so it must
    # come pre-enabled rather than sit on the global NULL/off default.
    demo.ensure_reference_seeded(db_session)
    pref = _reference_pref(db_session)
    assert pref is not None and pref.intraday_enabled is True


def test_ensure_reference_seeded_self_heals_stale_off_preference(db_session):
    # Simulates a deployment seeded before this default existed: portfolio present,
    # preference row present but off. A later startup call must correct it in place.
    demo.ensure_reference_seeded(db_session)
    pref = _reference_pref(db_session)
    pref.intraday_enabled = False
    db_session.commit()

    demo.ensure_reference_seeded(db_session)
    assert _reference_pref(db_session).intraday_enabled is True


def _live_sleeve_account_ids(db_session):
    """The live Crucible-synced sleeve's own accounts — excludes the permanently-frozen
    sample sleeve folded into the same portfolio (a distinct broker/source label,
    demo._SAMPLE_SLEEVE_SOURCE), so tests about the live sync's own persistence aren't
    thrown off by the sample sleeve's accounts sharing REFERENCE_PORTFOLIO_ID."""
    return db_session.scalars(
        select(models.Account.id).where(
            models.Account.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
            models.Account.broker == "reference",
        )
    ).all()


def test_sync_persists_holdings_sectors_and_nav(db_session):
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True

    acct_ids = _live_sleeve_account_ids(db_session)
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
    accts = _live_sleeve_account_ids(db_session)
    assert len(accts) == 1
    navs = db_session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == demo.REFERENCE_PORTFOLIO_ID)
    ).all()
    assert len(navs) == 3


def test_sync_failsoft_keeps_last_good(db_session):
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True
    # A subsequent failed sync (no artifact) must not blank the showcase.
    assert demo.sync_reference_holdings(db_session, reader=lambda: None) is False
    accts = _live_sleeve_account_ids(db_session)
    assert len(accts) == 1


def test_reference_nav_folds_in_sample_sleeve_totals(db_session):
    """The portfolio-level NavSnapshot (what Performance reads) must equal the live
    artifact's own NAV PLUS the frozen sample sleeve's constant total value — otherwise
    the persisted history undercounts what Holdings actually displays across both
    sleeves. The expected addon (28550.0 value / 25925.0 cost basis) is derived by hand
    from the frozen fixture (_SAMPLE_SLEEVE_CSV x _SAMPLE_SLEEVE_PRICES) as an
    independent cross-check on ``_sample_sleeve_totals``, not a copy of its own math."""
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True

    sample_value, sample_cost_basis = demo._sample_sleeve_totals(db_session)
    assert sample_value == 28_550.0
    assert sample_cost_basis == 25_925.0

    row = db_session.scalar(
        select(models.NavSnapshot).where(
            models.NavSnapshot.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
            models.NavSnapshot.snap_date == date(2026, 6, 18),
        )
    )
    assert float(row.nav) == 1_001_593.11 + sample_value


# ── Legacy "Demo portfolio" retirement ────────────────────────────────────────


def test_retire_legacy_demo_portfolio_noop_when_absent(db_session):
    demo.ensure_reference_seeded(db_session)  # must not raise when there's nothing to retire
    assert db_session.get(models.Portfolio, demo._LEGACY_DEMO_PORTFOLIO_ID) is None


def test_retire_legacy_demo_portfolio_cleans_up_orphan(db_session):
    # Simulate an already-deployed instance: the old standalone Demo portfolio, with an
    # account and both a portfolio-level and account-level NAV snapshot, still present.
    if db_session.get(models.Tenant, demo.DEMO_TENANT_ID) is None:
        db_session.add(models.Tenant(id=demo.DEMO_TENANT_ID, name="Demo"))
    db_session.add(
        models.Portfolio(
            id=demo._LEGACY_DEMO_PORTFOLIO_ID,
            tenant_id=demo.DEMO_TENANT_ID,
            name="Demo portfolio",
            base_currency="USD",
        )
    )
    db_session.commit()
    account = models.Account(
        tenant_id=demo.DEMO_TENANT_ID,
        portfolio_id=demo._LEGACY_DEMO_PORTFOLIO_ID,
        broker="csv",
        external_id="Demo Brokerage",
    )
    db_session.add(account)
    db_session.commit()
    db_session.add(
        models.AccountNavSnapshot(
            tenant_id=demo.DEMO_TENANT_ID,
            portfolio_id=demo._LEGACY_DEMO_PORTFOLIO_ID,
            account_id=account.id,
            snap_date=date(2024, 6, 28),
            nav=100.0,
        )
    )
    db_session.add(
        models.NavSnapshot(
            tenant_id=demo.DEMO_TENANT_ID,
            portfolio_id=demo._LEGACY_DEMO_PORTFOLIO_ID,
            snap_date=date(2024, 6, 28),
            nav=100.0,
        )
    )
    db_session.commit()

    demo.ensure_reference_seeded(db_session)

    assert db_session.get(models.Portfolio, demo._LEGACY_DEMO_PORTFOLIO_ID) is None
    assert db_session.get(models.Account, account.id) is None
    assert (
        db_session.scalars(
            select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == demo._LEGACY_DEMO_PORTFOLIO_ID)
        ).first()
        is None
    )
    assert (
        db_session.scalars(
            select(models.AccountNavSnapshot).where(models.AccountNavSnapshot.account_id == account.id)
        ).first()
        is None
    )


def test_daily_refresh_never_overwrites_reference_rate_nav(db_session, monkeypatch):
    """metron-ops#141 regression: daily_refresh's generic per-portfolio NAV writers
    (record_snapshot / record_account_snapshots / reconstruct_snapshots / reconcile_snapshots)
    must never touch the Reference Rate portfolio — its NavSnapshot series has exactly one
    authoritative source, the engine artifact seeded by sync_reference_holdings. Prices are
    monkeypatched to a value that deliberately DIFFERS wildly from the artifact's own NAV, so
    a regression (some writer re-deriving NAV from Metron's own price cache) would flip the
    assertion below instead of passing by coincidence."""
    assert demo.sync_reference_holdings(db_session, reader=_reader) is True

    today = date(2026, 6, 18)  # the artifact's own latest nav_history date
    wrong_close = ClosePoint(bar_date=today, close=1.0)  # absurd vs the artifact's real prices

    def _wrong_prices(symbols, *, source=None):
        return dict.fromkeys(symbols, wrong_close)

    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _wrong_prices)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _wrong_prices)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _wrong_prices)

    result = daily_refresh(db_session, today=today)

    assert result.snapshots_recorded == 0  # the only portfolio in the DB is the reference one
    row = db_session.scalar(
        select(models.NavSnapshot).where(
            models.NavSnapshot.portfolio_id == demo.REFERENCE_PORTFOLIO_ID,
            models.NavSnapshot.snap_date == today,
        )
    )
    # Untouched — still exactly the engine artifact's NAV plus the frozen sample sleeve's
    # constant addon, never Metron's re-derived figure off the (deliberately wrong) $1
    # closes above.
    sample_value, _sample_cost_basis = demo._sample_sleeve_totals(db_session)
    assert float(row.nav) == 1_001_593.11 + sample_value


_SAMPLE_SLEEVE_TICKERS = {"AAPL", "MSFT", "VOO", "912828YK0", "VMFXX"}


def test_holdings_endpoint_serves_reference(client, db_session):
    demo.sync_reference_holdings(db_session, reader=_reader)
    r = client.get(f"/portfolios/{demo.REFERENCE_PORTFOLIO_ID}/holdings", headers=REFERENCE_HEADERS)
    assert r.status_code == 200
    # The live sleeve's tickers, plus the frozen sample sleeve's asset-class breadth
    # folded into the same portfolio.
    assert {h["ticker"] for h in r.json()} == {"AMD", "SPY"} | _SAMPLE_SLEEVE_TICKERS


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
    assert {h["ticker"] for h in r.json()} == {"AMD", "SPY"} | _SAMPLE_SLEEVE_TICKERS


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
