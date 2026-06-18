"""Overview period-tile performance (metron-ops#83).

The Overview hero shows aggregate holdings performance over Today / YTD / LTM, each as a
$ investment gain + %TWR, plus a per-benchmark return and alpha. Benchmark comparison is
feed-gated: with_benchmarks=False (the no-feed beta) yields portfolio-only tiles. The
network is never hit — benchmark closes are seeded into the price_bars cache, and the
backfill fetch is monkeypatched to a no-op so a coverage gap can't reach out.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from api.db import models
from api.services import performance as perf
from api.services import prices as price_service


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _snap(session, tenant, pid, when, nav, *, flow=0.0):
    session.add(
        models.NavSnapshot(
            tenant_id=uuid.UUID(tenant),
            portfolio_id=pid,
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


# A clean 4-point series with no flows: 1000 → 1100 (year-end) → 1300 → 1320, so each
# window's TWR/gain is hand-checkable.
_SERIES = [
    (date(2023, 6, 1), 1000.0),
    (date(2023, 12, 31), 1100.0),
    (date(2024, 6, 29), 1300.0),
    (date(2024, 6, 30), 1320.0),
]
_TODAY = date(2024, 6, 30)


class TestWindows:
    def test_portfolio_only_windows(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False)

        assert res.benchmarks_available is False
        assert res.last_date == date(2024, 6, 30)
        tiles = {t.period: t for t in res.tiles}
        assert [t.period for t in res.tiles] == ["today", "ytd", "ltm"]

        # Today = the latest daily change (1300 → 1320).
        assert tiles["today"].gain == pytest.approx(20.0)
        assert tiles["today"].twr == pytest.approx(1320 / 1300 - 1)

        # YTD anchors on the prior year-end (1100) — 1320/1100 − 1 = 0.20.
        assert tiles["ytd"].start_date == date(2023, 12, 31)
        assert tiles["ytd"].gain == pytest.approx(220.0)
        assert tiles["ytd"].twr == pytest.approx(0.20)

        # LTM anchors on the last point on/before today−365d (2023-06-01, 1000) — 0.32.
        assert tiles["ltm"].start_date == date(2023, 6, 1)
        assert tiles["ltm"].gain == pytest.approx(320.0)
        assert tiles["ltm"].twr == pytest.approx(0.32)

        # Portfolio-only: no benchmark columns.
        assert all(t.benchmarks == [] for t in res.tiles)

    def test_twr_neutralizes_a_contribution_within_a_window(self, db_session, tenant):
        # A +200 deposit lands with the last point; the window TWR must neutralize it, but
        # the $ gain (net of flows) must also exclude it.
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2023, 12, 31), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 6, 29), 1100.0)
        _snap(db_session, tenant, pid, date(2024, 6, 30), 1410.0, flow=200.0)
        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False)
        ytd = next(t for t in res.tiles if t.period == "ytd")
        # Pre-flow last value = 1410 − 200 = 1210; TWR = (1100/1000)(1210/1100) − 1 = 0.21.
        assert ytd.twr == pytest.approx(0.21)
        # $ gain excludes the contribution: 1410 − 1000 − 200 = 210.
        assert ytd.gain == pytest.approx(210.0)


class TestBenchmarks:
    def test_benchmark_return_and_alpha(self, db_session, tenant, monkeypatch):
        # Fully-seeded coverage → the backfill fetch must never run; fail loudly if it does.
        monkeypatch.setattr(
            "api.services.prices.fetch_close_history",
            lambda *a, **k: pytest.fail("benchmark coverage hit the network"),
        )
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        spy_bars = [(date(2023, 6, 1), 400.0), (date(2023, 12, 31), 440.0), (date(2024, 6, 29), 480.0), (date(2024, 6, 30), 484.0)]
        for sym in ("SPY", "QQQ", "IWM"):
            _bars(db_session, sym, spy_bars)

        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=True)
        assert res.benchmarks_available is True
        tiles = {t.period: t for t in res.tiles}

        # All three proxies present, in canonical order.
        assert [b.symbol for b in tiles["ytd"].benchmarks] == ["SPY", "QQQ", "IWM"]

        spy_ytd = next(b for b in tiles["ytd"].benchmarks if b.symbol == "SPY")
        assert spy_ytd.ret == pytest.approx(484 / 440 - 1)  # 0.10
        assert spy_ytd.alpha == pytest.approx(0.20 - (484 / 440 - 1))

        spy_ltm = next(b for b in tiles["ltm"].benchmarks if b.symbol == "SPY")
        assert spy_ltm.ret == pytest.approx(484 / 400 - 1)  # 0.21
        assert spy_ltm.alpha == pytest.approx(0.32 - (484 / 400 - 1))

    def test_alpha_none_when_benchmark_uncached(self, db_session, tenant, monkeypatch):
        # No benchmark bars + the fetch is a no-op → benchmark cols present but ret/alpha None.
        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=True)
        assert res.benchmarks_available is False
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert {b.symbol for b in ytd.benchmarks} == {"SPY", "QQQ", "IWM"}
        assert all(b.ret is None and b.alpha is None for b in ytd.benchmarks)


class TestEmpty:
    def test_no_tiles_until_two_snapshots(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, _TODAY, 1000.0)
        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY)
        assert res.tiles == []
        assert res.last_date == _TODAY


class TestEndpoint:
    def test_tiles_endpoint_shape(self, client, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        puid = uuid.UUID(pid)
        _snap(db_session, tenant, puid, date(2024, 5, 1), 1000.0)
        _snap(db_session, tenant, puid, date(2024, 5, 31), 1100.0)
        for sym in ("SPY", "QQQ", "IWM"):
            _bars(db_session, sym, [(date(2024, 5, 1), 400.0), (date(2024, 5, 31), 440.0)])

        r = client.get(f"/portfolios/{pid}/performance/tiles", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert [t["period"] for t in body["tiles"]] == ["today", "ytd", "ltm"]
        # Feed is entitled by default (owner build) → benchmark columns populated.
        assert body["benchmarks_available"] is True
