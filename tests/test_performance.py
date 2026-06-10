"""NAV snapshots + performance metrics (C2-6b).

Performance is forward-recorded: each price refresh snapshots that day's NAV
(idempotent per day), and TWR / cumulative return derive from the accumulated series
via alpha_engine_lib.quant.returns. The network is never hit — the price source is
monkeypatched. Invariants: a snapshot is recorded only when NAV is valuable (never
fabricated); metrics are null until ≥2 snapshots; cash flows are neutralized in TWR.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from portfolio_analytics.prices import ClosePoint

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n"  # 10 sh, cost 1000

_AAPL = ClosePoint(bar_date=date(2024, 6, 3), close=150.0)  # MV 1500
_SPY = ClosePoint(bar_date=date(2024, 6, 3), close=500.0)


def _price_src(symbols, *, source=None):
    return {s: _AAPL for s in symbols if s == "AAPL"}


def _spy_src(symbols, *, source=None):
    return {"SPY": _SPY} if "SPY" in symbols else {}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


class TestSnapshotOnRefresh:
    def test_refresh_records_snapshot(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
        pid = _seed(client, tenant)

        body = client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant)).json()
        assert body["snapshot_recorded"] is True

        from api.db import models

        snap = db_session.scalars(select_navsnapshots(pid)).first()
        assert float(snap.nav) == 1500.0
        assert float(snap.cost_basis) == 1000.0
        assert float(snap.spy_close) == 500.0
        del models

    def test_no_snapshot_when_unpriceable(self, client, tenant, monkeypatch):
        # Source prices nothing → no NAV to record, snapshot_recorded False (no fabrication).
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", lambda s, *, source=None: {})
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
        pid = _seed(client, tenant)
        body = client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant)).json()
        assert body["snapshot_recorded"] is False

    def test_snapshot_idempotent_per_day(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _price_src)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", _spy_src)
        pid = _seed(client, tenant)
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        client.post(f"/portfolios/{pid}/prices/refresh", headers=_hdr(tenant))
        assert len(db_session.scalars(select_navsnapshots(pid)).all()) == 1


class TestPerformanceMetrics:
    def test_empty_until_two_snapshots(self, client, tenant):
        pid = _seed(client, tenant)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["n_snapshots"] == 0 and p["twr"] is None and p["cumulative_return"] is None

    def test_twr_and_cumulative_over_series(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 31), nav=1100.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["n_snapshots"] == 2
        assert p["days"] == 30
        assert p["cumulative_return"] == pytest.approx(0.10)  # (1100-0)/1000 - 1
        assert p["twr"] == pytest.approx(0.10)                # 1100/1000 - 1, no flows
        assert p["annualized_twr"] == pytest.approx(1.10 ** (365 / 30) - 1)

    def test_twr_neutralizes_a_contribution(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        # Start 1000; a +500 deposit lands with day-2's snapshot, NAV 1600 → the 100 of
        # growth (1500→1600 on invested 1500... ) must not read as a 60% gain.
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 31), nav=1600.0, external_flow=500.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # Pre-flow value on d2 = 1600 − 500 = 1100; TWR sub-period = 1100/1000 − 1 = 0.10
        # (the 500 deposit is neutralized, NOT read as a 60% gain).
        assert p["twr"] == pytest.approx(0.10)
        # cumulative is flow-adjusted: (1600 − 500)/1000 − 1 = 0.10.
        assert p["cumulative_return"] == pytest.approx(0.10)
        assert p["net_contributions"] == pytest.approx(500.0)


class TestRiskAndAlpha:
    def test_alpha_and_risk_metrics(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0, spy_close=400.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0, spy_close=420.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 3, 1), nav=1050.0, spy_close=410.0)  # a dip → variance + dd
        _insert_snapshot(db_session, tenant, pid, date(2024, 4, 1), nav=1200.0, spy_close=440.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        assert p["spy_return"] == pytest.approx(440 / 400 - 1)
        assert p["twr"] == pytest.approx(0.20)  # (1.1)(1050/1100)(1200/1050) − 1
        assert p["alpha"] == pytest.approx(p["twr"] - p["spy_return"])
        assert p["max_drawdown"] == pytest.approx(1050 / 1100 - 1)  # peak 1.10 → trough 1.05
        assert p["volatility"] is not None and p["volatility"] > 0
        assert p["sharpe"] is not None and p["sortino"] is not None

    def test_alpha_without_enough_history_for_risk(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0, spy_close=400.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0, spy_close=440.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # 2 snapshots → alpha computable, but vol/Sharpe need ≥2 returns (≥3 snapshots).
        assert p["alpha"] == pytest.approx(p["twr"] - p["spy_return"])
        assert p["volatility"] is None and p["sharpe"] is None

    def test_drawdown_is_flow_neutralized(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        _insert_snapshot(db_session, tenant, pid, date(2024, 1, 1), nav=1000.0)
        _insert_snapshot(db_session, tenant, pid, date(2024, 2, 1), nav=1100.0)
        # Withdrew 500 with no investment loss: NAV 1100 → 600, external_flow −500.
        _insert_snapshot(db_session, tenant, pid, date(2024, 3, 1), nav=600.0, external_flow=-500.0)
        p = client.get(f"/portfolios/{pid}/performance", headers=_hdr(tenant)).json()
        # Flow-neutralized d3 return = (600 − (−500))/1100 − 1 = 0 → NOT a 45% drawdown.
        assert p["max_drawdown"] == pytest.approx(0.0)


# --- helpers ---------------------------------------------------------------


def select_navsnapshots(portfolio_id):
    from sqlalchemy import select

    from api.db import models

    return select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == uuid.UUID(portfolio_id))


def _insert_snapshot(session, tenant, portfolio_id, when, *, nav, external_flow=0.0, spy_close=None):
    from api.db import models

    session.add(
        models.NavSnapshot(
            tenant_id=uuid.UUID(tenant),
            portfolio_id=uuid.UUID(portfolio_id),
            snap_date=when,
            nav=nav,
            cost_basis=1000.0,
            external_flow=external_flow,
            spy_close=spy_close,
        )
    )
    session.commit()
