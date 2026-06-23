"""Per-account performance series for the Holdings chart (metron-ops#78).

One cumulative flow-neutralized growth index per account (g[0]=1.0), plus feed-gated
SPY/QQQ/IWM benchmark overlays normalized the same way. The network is never hit —
benchmark closes are seeded and the backfill fetch is monkeypatched to fail loudly.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from api.db import models
from api.services import performance as perf
from api.services import prices as price_service

_TODAY = date(2024, 6, 30)


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _account(session, tenant, pid, name):
    aid = uuid.uuid4()
    session.add(
        models.Account(id=aid, tenant_id=uuid.UUID(tenant), portfolio_id=pid, broker="csv", external_id=name, name=name)
    )
    session.commit()
    return aid


def _asnap(session, tenant, pid, aid, when, nav, *, flow=0.0):
    session.add(
        models.AccountNavSnapshot(
            tenant_id=uuid.UUID(tenant),
            portfolio_id=pid,
            account_id=aid,
            snap_date=when,
            nav=nav,
            cost_basis=1000.0,
            external_flow=flow,
            spy_close=None,
        )
    )
    session.commit()


def _bars(session, symbol, bars):
    sid = price_service.ensure_security(session, symbol)
    for when, close in bars:
        session.add(models.PriceBar(security_id=sid, bar_date=when, close=close, currency="USD"))
    session.commit()


_DATES = [date(2024, 1, 31), date(2024, 3, 31), date(2024, 5, 31)]


class TestAccountSeries:
    def test_growth_index_and_benchmarks(self, db_session, tenant, monkeypatch):
        monkeypatch.setattr(
            "api.services.prices.fetch_close_history",
            lambda *a, **k: pytest.fail("benchmark coverage hit the network"),
        )
        pid = uuid.uuid4()
        a1 = _account(db_session, tenant, pid, "Brokerage")
        a2 = _account(db_session, tenant, pid, "IRA")
        for when, nav in zip(_DATES, [1000.0, 1100.0, 1210.0], strict=True):
            _asnap(db_session, tenant, pid, a1, when, nav)
        for when, nav in zip(_DATES, [2000.0, 1900.0, 2090.0], strict=True):
            _asnap(db_session, tenant, pid, a2, when, nav)
        for sym in ("SPY", "QQQ", "IWM"):
            _bars(db_session, sym, list(zip(_DATES, [400.0, 440.0, 484.0], strict=True)))

        # today = the last data date so seeded coverage is fresh (no backfill / network).
        res = perf.account_performance_series(db_session, uuid.UUID(tenant), pid, today=_DATES[-1], with_benchmarks=True)

        by_name = {a.name: a for a in res.accounts}
        assert set(by_name) == {"Brokerage", "IRA"}
        # Brokerage: 1000 → 1100 → 1210, no flows → g = 1.0, 1.1, 1.21.
        g1 = [round(p.g, 4) for p in by_name["Brokerage"].points]
        assert g1 == [1.0, 1.1, 1.21]
        assert by_name["Brokerage"].points[0].when == date(2024, 1, 31)

        assert res.benchmarks_available is True
        spy = next(b for b in res.benchmarks if b.symbol == "SPY")
        assert [round(p.g, 4) for p in spy.points] == [1.0, 1.1, 1.21]  # 400/400, 440/400, 484/400

    def test_growth_neutralizes_a_contribution(self, db_session, tenant):
        pid = uuid.uuid4()
        a1 = _account(db_session, tenant, pid, "Brokerage")
        _asnap(db_session, tenant, pid, a1, _DATES[0], 1000.0)
        # +500 deposit lands with the second snapshot (NAV 1600) — growth must be 1.1, not 1.6.
        _asnap(db_session, tenant, pid, a1, _DATES[1], 1600.0, flow=500.0)
        res = perf.account_performance_series(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False)
        g = [round(p.g, 4) for p in res.accounts[0].points]
        assert g == [1.0, 1.1]  # (1600 − 500)/1000 = 1.10

    def test_feed_gated_off_yields_account_lines_only(self, db_session, tenant):
        pid = uuid.uuid4()
        a1 = _account(db_session, tenant, pid, "Brokerage")
        for when, nav in zip(_DATES, [1000.0, 1100.0, 1210.0], strict=True):
            _asnap(db_session, tenant, pid, a1, when, nav)
        res = perf.account_performance_series(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False)
        assert len(res.accounts) == 1
        assert res.benchmarks == []
        assert res.benchmarks_available is False

    def test_single_point_account_is_not_a_line(self, db_session, tenant):
        pid = uuid.uuid4()
        a1 = _account(db_session, tenant, pid, "Brokerage")
        _asnap(db_session, tenant, pid, a1, _DATES[0], 1000.0)  # one point only
        res = perf.account_performance_series(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False)
        assert res.accounts == []

    def test_selection_scopes_to_chosen_accounts(self, db_session, tenant):
        pid = uuid.uuid4()
        a1 = _account(db_session, tenant, pid, "Brokerage")
        a2 = _account(db_session, tenant, pid, "IRA")
        for aid in (a1, a2):
            for when, nav in zip(_DATES, [1000.0, 1100.0, 1210.0], strict=True):
                _asnap(db_session, tenant, pid, aid, when, nav)
        res = perf.account_performance_series(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, account_ids=[a1], with_benchmarks=False
        )
        assert [a.account_id for a in res.accounts] == [a1]


class TestReconstructedCoverage:
    """A reconstructable account (CSV ledger / IBKR lots) gets a DEEP line tagged
    coverage="reconstructed"; a snapshot-sourced account with no lots stays "forward"
    (metron-ops#87)."""

    def test_csv_account_is_reconstructed_deep(self, db_session, tenant):
        pid = uuid.uuid4()
        aid = _account(db_session, tenant, pid, "Old")  # broker="csv"
        sec = models.Security(symbol="AAPL", currency="USD")
        db_session.add(sec)
        db_session.flush()
        db_session.add(
            models.Transaction(
                tenant_id=uuid.UUID(tenant), account_id=aid, security_id=sec.id,
                txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
                trade_date=date(2024, 1, 2), source_key="buy-1",
            )
        )
        db_session.commit()
        # Cached closes only — backfill=False on read must not hit the network.
        _bars(db_session, "AAPL", [(date(2024, 1, 2), 100.0), (date(2024, 3, 1), 120.0), (date(2024, 6, 28), 150.0)])

        res = perf.account_performance_series(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 6, 30), with_benchmarks=False
        )
        acct = res.accounts[0]
        assert acct.coverage == "reconstructed"
        assert acct.points[0].when == date(2024, 1, 2)  # deep — back to the first lot, not forward-only
        assert round(acct.points[-1].g, 4) == 1.5  # 10×150 / 10×100

    def test_snapshot_account_without_lots_stays_forward(self, db_session, tenant):
        pid = uuid.uuid4()
        aid = uuid.uuid4()
        db_session.add(
            models.Account(id=aid, tenant_id=uuid.UUID(tenant), portfolio_id=pid, broker="snaptrade", external_id="S1", name="SnapTrade")
        )
        db_session.commit()
        for when, nav in zip(_DATES, [1000.0, 1100.0, 1210.0], strict=True):
            _asnap(db_session, tenant, pid, aid, when, nav)
        res = perf.account_performance_series(
            db_session, uuid.UUID(tenant), pid, today=_DATES[-1], with_benchmarks=False
        )
        assert res.accounts[0].coverage == "forward"


class TestAccountPeriodReturns:
    """Per-account Day/YTD/LTM rollups (metron-ops#87): YTD/LTM from the reconstructed
    per-account NAV series; Day legs need the feed (None off a feed-entitled build)."""

    def test_ytd_ltm_from_reconstructed_series(self, db_session, tenant):
        pid = uuid.uuid4()
        aid = _account(db_session, tenant, pid, "Old")  # broker="csv" → reconstructable
        sec = models.Security(symbol="AAPL", currency="USD")
        db_session.add(sec)
        db_session.flush()
        db_session.add(
            models.Transaction(
                tenant_id=uuid.UUID(tenant), account_id=aid, security_id=sec.id,
                txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
                trade_date=date(2024, 1, 2), source_key="buy-1",
            )
        )
        db_session.commit()
        _bars(db_session, "AAPL", [(date(2024, 1, 2), 100.0), (date(2025, 1, 2), 150.0), (date(2025, 6, 26), 180.0)])

        out = perf.account_period_returns(
            db_session, uuid.UUID(tenant), pid, today=date(2025, 6, 27), feed_entitled=False
        )
        r = out[aid]
        assert r.ytd_pct == pytest.approx(0.20, abs=1e-3)  # 180/150 − 1 (latest vs first 2025 close)
        assert r.ltm_pct == pytest.approx(0.80, abs=1e-3)  # 180/100 − 1 (latest vs ~1y-ago close)
        assert r.day_pct is None  # no feed → no Day legs


class TestEndpoint:
    def test_endpoint_shape(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        puid = uuid.UUID(pid)
        a1 = _account(db_session, tenant, puid, "Brokerage")
        for when, nav in zip(_DATES, [1000.0, 1100.0, 1210.0], strict=True):
            _asnap(db_session, tenant, puid, a1, when, nav)
        for sym in ("SPY", "QQQ", "IWM"):
            _bars(db_session, sym, list(zip(_DATES, [400.0, 440.0, 484.0], strict=True)))

        r = client.get(f"/portfolios/{pid}/holdings/performance-series", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert len(body["accounts"]) == 1
        assert body["accounts"][0]["points"][0]["g"] == 1.0
        assert body["benchmarks_available"] is True
