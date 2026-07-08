"""The daily-refresh maintenance job: prices + NAV snapshots across all portfolios."""

from __future__ import annotations

import io
import types
import uuid
from datetime import date

from sqlalchemy import select

from api.db import models
from api.maintenance import daily_refresh, main
from portfolio_analytics.prices import ClosePoint


def _no_derived(monkeypatch):
    """Neutralize the best-effort derived backfills (reconstruct / risk / attribution)
    so a test exercising the price+snapshot primary path stays hermetic + deterministic
    (they'd otherwise hit real yfinance and add reconstructed snapshots)."""
    monkeypatch.setattr("api.maintenance.performance.reconstruct_snapshots", lambda *a, **k: 0)
    monkeypatch.setattr("api.maintenance.risk.compute_risk", lambda *a, **k: types.SimpleNamespace(computable=False))
    monkeypatch.setattr(
        "api.maintenance.attribution.compute_attribution", lambda *a, **k: types.SimpleNamespace(computable=False)
    )
    monkeypatch.setattr("api.maintenance.calendar_svc.refresh_earnings", lambda *a, **k: 0)

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n"  # 10 sh, cost 1000
_AAPL = ClosePoint(bar_date=date(2024, 6, 3), close=150.0)  # MV 1500
_SPY = ClosePoint(bar_date=date(2024, 6, 3), close=500.0)


def _price_src(symbols, *, source=None):
    return {s: _AAPL for s in symbols if s == "AAPL"}


def _spy_src(symbols, *, source=None):
    return {"SPY": _SPY} if "SPY" in symbols else {}


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant, name="P"):
    pid = client.post("/portfolios", json={"name": name}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def _navsnaps(pid):
    return select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == uuid.UUID(pid))


def test_daily_refresh_records_snapshots_for_all_portfolios(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
    _no_derived(monkeypatch)
    # Two tenants, one portfolio each — the operator job sweeps both.
    t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
    p1 = _seed(client, t1, "A")
    p2 = _seed(client, t2, "B")

    result = daily_refresh(db_session, today=date(2024, 6, 3))

    assert result.portfolios == 2
    assert result.snapshots_recorded == 2
    assert result.symbols == 2  # one AAPL holding each
    for pid in (p1, p2):
        snap = db_session.scalars(_navsnaps(pid)).first()
        assert float(snap.nav) == 1500.0
        assert float(snap.spy_close) == 500.0


def test_daily_refresh_idempotent_per_day(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
    _no_derived(monkeypatch)
    t = str(uuid.uuid4())
    pid = _seed(client, t)
    daily_refresh(db_session, today=date(2024, 6, 3))
    daily_refresh(db_session, today=date(2024, 6, 3))
    assert len(db_session.scalars(_navsnaps(pid)).all()) == 1  # same day → one row


def test_daily_refresh_skips_unpriceable_without_fabricating(client, db_session, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", lambda s, *, source=None: {})
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", lambda s, *, source=None: {})
    _no_derived(monkeypatch)
    t = str(uuid.uuid4())
    _seed(client, t)
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result.portfolios == 1
    assert result.snapshots_recorded == 0  # nothing priceable → no fabricated NAV (gate passes; record_snapshot skips)


def test_daily_refresh_populates_derived_pages(client, db_session, monkeypatch):
    # The job pre-computes Performance / Risk / Attribution so those pages flow data
    # without a manual "Compute" click. Stub the heavy backfills to assert the wiring.
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.performance.reconstruct_snapshots", lambda *a, **k: 5)
    monkeypatch.setattr("api.maintenance.risk.compute_risk", lambda *a, **k: types.SimpleNamespace(computable=True))
    monkeypatch.setattr(
        "api.maintenance.attribution.compute_attribution", lambda *a, **k: types.SimpleNamespace(computable=True)
    )
    monkeypatch.setattr("api.maintenance.calendar_svc.refresh_earnings", lambda *a, **k: 7)
    _seed(client, str(uuid.uuid4()))
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result.snapshots_reconstructed == 5
    assert result.risk_computed == 1
    assert result.attribution_computed == 1
    assert result.earnings_refreshed == 7  # earnings auto-pulled from the spine (metron-ops#76)


def test_daily_refresh_derived_backfill_is_best_effort(client, db_session, monkeypatch):
    # A yfinance failure in any derived backfill must NOT cost the price refresh / NAV
    # snapshot (which have already committed) — it logs a WARN and the job continues.
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)

    def boom(*a, **k):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr("api.maintenance.performance.reconstruct_snapshots", boom)
    monkeypatch.setattr("api.maintenance.risk.compute_risk", boom)
    monkeypatch.setattr("api.maintenance.attribution.compute_attribution", boom)
    monkeypatch.setattr("api.maintenance.calendar_svc.refresh_earnings", boom)
    _seed(client, str(uuid.uuid4()))
    result = daily_refresh(db_session, today=date(2024, 6, 3))  # must not raise
    assert result.snapshots_recorded == 1  # primary path survived
    assert result.snapshots_reconstructed == 0
    assert result.risk_computed == 0 and result.attribution_computed == 0
    assert result.earnings_refreshed == 0  # earnings failure is best-effort too


def test_daily_refresh_empty_db_is_a_noop(db_session, monkeypatch):
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)  # no network for the freshness probe
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result == type(result)(portfolios=0, symbols=0, prices_updated=0, snapshots_recorded=0)


def test_daily_refresh_defers_snapshot_until_todays_close_published(client, db_session, monkeypatch):
    """The freshness gate: a weekday run BEFORE today's close prints in the spine must NOT
    record a today-stamped snapshot on yesterday's prices — it defers, leaving the Today
    tile honestly on the prior session until a later fire records the true value."""
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    # SPY's freshest close is the PRIOR session (today's hasn't published) → defer.
    stale_spy = {"SPY": ClosePoint(bar_date=date(2024, 5, 31), close=500.0)}  # Fri before Mon 6/3
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", lambda s, *, source=None: stale_spy)
    _no_derived(monkeypatch)
    pid = _seed(client, str(uuid.uuid4()))

    result = daily_refresh(db_session, today=date(2024, 6, 3))  # Monday, pre-publish

    assert result.snapshots_recorded == 0
    assert result.snapshots_deferred == 1  # surfaced for observability
    assert db_session.scalars(_navsnaps(pid)).first() is None  # nothing stamped under today

    # Once today's close publishes, a later fire records it (same calendar day, idempotent).
    fresh_spy = {"SPY": ClosePoint(bar_date=date(2024, 6, 3), close=500.0)}
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", lambda s, *, source=None: fresh_spy)
    result2 = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result2.snapshots_recorded == 1
    assert result2.snapshots_deferred == 0
    snap = db_session.scalars(_navsnaps(pid)).first()
    assert float(snap.nav) == 1500.0


def test_daily_refresh_weekend_run_is_not_gated(client, db_session, monkeypatch):
    """Weekends have no new session, so the gate is a no-op — the carry-forward snapshot
    (last close) is still recorded even though SPY's bar_date predates 'today'."""
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    fri_spy = {"SPY": ClosePoint(bar_date=date(2024, 5, 31), close=500.0)}
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", lambda s, *, source=None: fri_spy)
    _no_derived(monkeypatch)
    pid = _seed(client, str(uuid.uuid4()))

    result = daily_refresh(db_session, today=date(2024, 6, 1))  # Saturday

    assert result.snapshots_recorded == 1
    assert result.snapshots_deferred == 0
    assert db_session.scalars(_navsnaps(pid)).first() is not None


def test_daily_refresh_syncs_broker_positions_and_counts_them(client, db_session, monkeypatch):
    # Wiring check for metron-ops#150: the broker re-sync runs per portfolio, before
    # holdings/prices are computed, and a successful sync is counted in the result.
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
    _no_derived(monkeypatch)
    calls = []

    def _fake_flex(session, portfolio):
        calls.append(portfolio.id)
        return object()  # any non-None return counts as "synced"

    monkeypatch.setattr("api.maintenance.broker_sync.sync_flex_for_portfolio", _fake_flex)
    monkeypatch.setattr("api.maintenance.broker_sync.sync_snaptrade_for_portfolio", lambda s, p: None)
    _seed(client, str(uuid.uuid4()))
    result = daily_refresh(db_session, today=date(2024, 6, 3))
    assert result.broker_flex_synced == 1
    assert result.broker_snaptrade_synced == 0
    assert len(calls) == 1


def test_daily_refresh_broker_sync_failure_is_best_effort(client, db_session, monkeypatch):
    # A Flex/SnapTrade outage must not cost the price refresh / NAV snapshot, which have
    # already committed by the time the derived backfills run — same posture as the
    # existing derived-backfill best-effort guarantee.
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)
    _no_derived(monkeypatch)

    def _boom(session, portfolio):
        raise RuntimeError("Flex outage")

    monkeypatch.setattr("api.maintenance.broker_sync.sync_flex_for_portfolio", _boom)
    monkeypatch.setattr("api.maintenance.broker_sync.sync_snaptrade_for_portfolio", lambda s, p: None)
    _seed(client, str(uuid.uuid4()))
    result = daily_refresh(db_session, today=date(2024, 6, 3))  # must not raise
    assert result.broker_flex_synced == 0
    assert result.snapshots_recorded == 1  # primary path survived the broker-sync failure


def test_daily_refresh_skips_broker_sync_for_reference_rate_portfolio(db_session, monkeypatch):
    # The reference-rate showcase has no real brokerage attached (its NAV is sole-sourced
    # from the engine's artifact, metron-ops#141) — the broker re-sync must never probe it.
    from api.services.demo import REFERENCE_PORTFOLIO_ID

    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy_src)

    def _fail_if_called(*a, **k):
        raise AssertionError("broker sync must be skipped for the reference-rate portfolio")

    monkeypatch.setattr("api.maintenance.broker_sync.sync_flex_for_portfolio", _fail_if_called)
    monkeypatch.setattr("api.maintenance.broker_sync.sync_snaptrade_for_portfolio", _fail_if_called)
    tenant = models.Tenant(name="ref")
    db_session.add(tenant)
    db_session.flush()
    db_session.add(models.Portfolio(id=REFERENCE_PORTFOLIO_ID, tenant_id=tenant.id, name="Reference Rate"))
    db_session.commit()
    result = daily_refresh(db_session, today=date(2024, 6, 3))  # must not raise
    assert result.portfolios == 1


def test_cli_unknown_command_errors():
    import pytest

    with pytest.raises(SystemExit):
        main(["bogus"])
