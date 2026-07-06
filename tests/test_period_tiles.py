"""Overview period-tile performance (metron-ops#83).

The Overview hero shows aggregate holdings performance over Today / YTD / LTM, each as a
$ investment gain + %TWR, plus a per-benchmark return and alpha. Benchmark comparison is
feed-gated: with_benchmarks=False (the no-feed beta) yields portfolio-only tiles. The
network is never hit — benchmark closes are seeded into the price_bars cache, and the
backfill fetch is monkeypatched to a no-op so a coverage gap can't reach out.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from api.db import models
from api.services import performance as perf
from api.services import prices as price_service

_ET = ZoneInfo("America/New_York")


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


def _txn(session, tenant, pid, when, txn_type, amount):
    """A BUY/SELL on ``when`` for ``amount`` (the external-capital flow `_net_purchases`
    neutralizes) — needs an Account under ``pid`` for the portfolio join."""
    aid = uuid.uuid4()
    session.add(
        models.Account(
            id=aid, tenant_id=uuid.UUID(tenant), portfolio_id=pid,
            broker="csv", external_id=f"acct-{when}-{txn_type}", name="A",
        )
    )
    sec = models.Security(symbol="AAPL", currency="USD")
    session.add(sec)
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=uuid.UUID(tenant), account_id=aid, security_id=sec.id,
            txn_type=txn_type, quantity=1, price=amount, amount=amount, currency="USD",
            trade_date=when, source_key=f"{txn_type}-{when}",
        )
    )
    session.commit()


# Trading-day NAV snapshots only (NYSE session dates) — hand-checkable windows.
_SERIES = [
    (date(2023, 6, 1), 1000.0),
    (date(2023, 12, 29), 1100.0),  # Fri — last trading day of 2023
    (date(2024, 6, 26), 1280.0),   # Wed
    (date(2024, 6, 27), 1300.0),   # Thu
    (date(2024, 6, 28), 1320.0),   # Fri
]
_TODAY = date(2024, 6, 28)
_FRIDAY_POST_CLOSE = datetime(2024, 6, 28, 17, 0, tzinfo=_ET)
_MONDAY_MIDDAY = datetime(2024, 7, 1, 12, 0, tzinfo=_ET)


class TestWindows:
    def test_portfolio_only_windows(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False, now=_FRIDAY_POST_CLOSE,
        )

        assert res.benchmarks_available is False
        assert res.last_date == date(2024, 6, 28)
        tiles = {t.period: t for t in res.tiles}
        assert [t.period for t in res.tiles] == ["today", "ytd", "ltm"]

        # Today = Fri close-to-close (Thu 1300 → Fri 1320).
        assert tiles["today"].gain == pytest.approx(20.0)
        assert tiles["today"].twr == pytest.approx(1320 / 1300 - 1)

        # YTD anchors on the prior year-end trading day (1100) — 1320/1100 − 1 = 0.20.
        assert tiles["ytd"].start_date == date(2023, 12, 29)
        assert tiles["ytd"].gain == pytest.approx(220.0)
        assert tiles["ytd"].twr == pytest.approx(0.20)

        # LTM anchors on the last point on/before today−365d (2023-06-01, 1000) — 0.32.
        assert tiles["ltm"].start_date == date(2023, 6, 1)
        assert tiles["ltm"].gain == pytest.approx(320.0)
        assert tiles["ltm"].twr == pytest.approx(0.32)

        assert all(t.benchmarks == [] for t in res.tiles)

    def test_twr_neutralizes_a_contribution_within_a_window(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2023, 12, 29), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 6, 27), 1100.0)
        _snap(db_session, tenant, pid, date(2024, 6, 28), 1410.0, flow=200.0)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False, now=_FRIDAY_POST_CLOSE,
        )
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert ytd.twr == pytest.approx(0.21)
        assert ytd.gain == pytest.approx(210.0)


class TestBenchmarks:
    def test_benchmark_return_and_alpha(self, db_session, tenant, monkeypatch):
        monkeypatch.setattr(
            "api.services.prices.fetch_close_history",
            lambda *a, **k: pytest.fail("benchmark coverage hit the network"),
        )
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        spy_bars = [
            (date(2023, 6, 1), 400.0), (date(2023, 12, 29), 440.0),
            (date(2024, 6, 27), 480.0), (date(2024, 6, 28), 484.0),
        ]
        for sym in ("SPY", "QQQ", "IWM"):
            _bars(db_session, sym, spy_bars)

        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=True, now=_FRIDAY_POST_CLOSE,
        )
        assert res.benchmarks_available is True
        tiles = {t.period: t for t in res.tiles}
        assert [b.symbol for b in tiles["ytd"].benchmarks] == ["SPY", "QQQ", "IWM"]

        spy_ytd = next(b for b in tiles["ytd"].benchmarks if b.symbol == "SPY")
        assert spy_ytd.ret == pytest.approx(484 / 440 - 1)
        assert spy_ytd.alpha == pytest.approx(0.20 - (484 / 440 - 1))

        spy_ltm = next(b for b in tiles["ltm"].benchmarks if b.symbol == "SPY")
        assert spy_ltm.ret == pytest.approx(484 / 400 - 1)
        assert spy_ltm.alpha == pytest.approx(0.32 - (484 / 400 - 1))

    def test_alpha_none_when_benchmark_uncached(self, db_session, tenant, monkeypatch):
        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=True, now=_FRIDAY_POST_CLOSE,
        )
        assert res.benchmarks_available is False
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert {b.symbol for b in ytd.benchmarks} == {"SPY", "QQQ", "IWM"}
        assert all(b.ret is None and b.alpha is None for b in ytd.benchmarks)


class TestTodayDateGuard:
    """When the last closed session predates the current NYSE session, TODAY shows that
    session's close-to-close change labeled "as of <date>". When the last closed session
    IS the current session (post-close), no label."""

    def test_today_shows_latest_close_with_as_of_label_when_snapshot_predates_today(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        # Mon pre-open: last closed = Fri 6/28 (Thu 1300 → Fri 1320).
        assert today.gain == pytest.approx(20.0)
        assert today.twr == pytest.approx(1320 / 1300 - 1)
        assert today.end_date == date(2024, 6, 28)
        assert today.note == "as of 2024-06-28"
        assert today.intraday is False
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert ytd.gain is not None and ytd.note is None

    def test_today_forms_when_latest_snapshot_is_today(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False, now=_FRIDAY_POST_CLOSE,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.gain == pytest.approx(20.0)
        assert today.note is None


class TestLongWeekend:
    """Weekend carry-forward snapshots at an unchanged NAV must not zero out TODAY."""

    def test_today_uses_last_trading_session_not_weekend_stamps(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2026, 7, 1), 1000.0)   # Wed
        _snap(db_session, tenant, pid, date(2026, 7, 2), 1050.0)   # Thu +50
        _snap(db_session, tenant, pid, date(2026, 7, 4), 1050.0)   # Sat carry-forward
        _snap(db_session, tenant, pid, date(2026, 7, 5), 1050.0)   # Sun carry-forward
        mon_preopen = datetime(2026, 7, 6, 10, 0, tzinfo=_ET)  # Mon after 7/3 holiday
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2026, 7, 6), with_benchmarks=False, now=mon_preopen,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.gain == pytest.approx(50.0)
        assert today.end_date == date(2026, 7, 2)
        assert today.note == "as of 2026-07-02"


class TestLiveIntradayToday:
    """The TODAY tile is a live intraday number (metron-ops#95) when the overlay is in
    effect: prior trading session's close → current live NAV, flow-neutralized."""

    def test_intraday_today_anchors_prior_session_to_live_nav(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        live = perf.LiveToday(
            nav=1353.0,
            intraday_applied=True,
            as_of_utc="2024-07-01T17:30:00Z",
            bench={"SPY": (101.0, 100.0), "QQQ": (None, None), "IWM": (200.0, 0.0)},
        )
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=True,
            live=live, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.intraday is True
        assert today.note is None
        assert today.start_date == date(2024, 6, 28)
        assert today.end_date == date(2024, 7, 1)
        assert today.gain == pytest.approx(33.0)
        assert today.twr == pytest.approx(1353 / 1320 - 1)
        spy = next(b for b in today.benchmarks if b.symbol == "SPY")
        assert spy.ret == pytest.approx(0.01)
        assert next(b for b in today.benchmarks if b.symbol == "QQQ").ret is None
        assert next(b for b in today.benchmarks if b.symbol == "IWM").ret is None

    def test_intraday_today_neutralizes_a_same_day_flow(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2024, 6, 27), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 6, 28), 1200.0)  # Fri prior close
        _txn(db_session, tenant, pid, date(2024, 7, 1), "BUY", 300.0)
        live = perf.LiveToday(nav=1530.0, intraday_applied=True)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False,
            live=live, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.gain == pytest.approx(30.0)
        assert today.twr == pytest.approx(1230 / 1200 - 1)

    def test_falls_back_to_snapshot_path_when_overlay_not_applied(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        live = perf.LiveToday(nav=9999.0, intraday_applied=False)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False,
            live=live, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.intraday is False
        assert today.gain == pytest.approx(20.0) and today.note == "as of 2024-06-28"

    def test_intraday_skipped_when_no_prior_session_to_anchor(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2024, 6, 28), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 7, 1), 1010.0)
        live = perf.LiveToday(nav=1020.0, intraday_applied=True)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False,
            live=live, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.intraday is True
        assert today.start_date == date(2024, 6, 28)
        assert today.gain == pytest.approx(20.0)


class TestTodaySettledBenchmarkFromIndexStrip:
    def test_uses_index_strip_quote_over_a_stale_price_bars_cache(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        _bars(db_session, "SPY", [
            (date(2023, 6, 1), 400.0), (date(2023, 12, 29), 440.0),
            (date(2024, 6, 27), 480.0), (date(2024, 6, 28), 470.0),
        ])
        for sym in ("QQQ", "IWM"):
            _bars(db_session, sym, [
                (date(2023, 6, 1), 300.0), (date(2023, 12, 29), 330.0), (date(2024, 6, 26), 300.0),
            ])
        today_bench = {"SPY": (99.0, 100.0), "QQQ": (95.0, 100.0), "IWM": (98.0, 100.0)}
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=True,
            today_bench=today_bench, now=_FRIDAY_POST_CLOSE,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.note is None
        assert next(b for b in today.benchmarks if b.symbol == "SPY").ret == pytest.approx(-0.01)
        assert next(b for b in today.benchmarks if b.symbol == "QQQ").ret == pytest.approx(-0.05)
        assert next(b for b in today.benchmarks if b.symbol == "IWM").ret == pytest.approx(-0.02)
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert next(b for b in ytd.benchmarks if b.symbol == "QQQ").ret == pytest.approx(300 / 330 - 1)

    def test_falls_back_to_price_bars_when_the_settled_today_tile_predates_today(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        _bars(db_session, "SPY", [(date(2024, 6, 27), 480.0), (date(2024, 6, 28), 484.0)])
        today_bench = {"SPY": (99.0, 100.0)}
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=True,
            today_bench=today_bench, now=_MONDAY_MIDDAY,
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.note == "as of 2024-06-28"
        spy = next(b for b in today.benchmarks if b.symbol == "SPY")
        assert spy.ret == pytest.approx(484 / 480 - 1)


class TestEmpty:
    def test_no_tiles_until_two_snapshots(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, _TODAY, 1000.0)
        res = perf.period_tiles(db_session, uuid.UUID(tenant), pid, today=_TODAY, now=_FRIDAY_POST_CLOSE)
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
        assert body["benchmarks_available"] is True

    def test_endpoint_wires_index_strip_into_the_settled_today_tile(self, client, db_session, tenant, monkeypatch):
        from api.services import indices as indices_service

        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        session_day = date(2024, 6, 28)
        monkeypatch.setattr(
            indices_service, "load_indices",
            lambda: indices_service.IndicesSnapshot(
                True, as_of_utc="2024-06-28T21:00:00Z",
                indices=[
                    indices_service.IndexQuote(
                        "QQQ", "Nasdaq 100", last=95.0, prev_close=100.0, open=100.0,
                        change=-5.0, change_pct=-0.05, session_date=session_day.isoformat(), suspect=False,
                    ),
                ],
            ),
        )

        _orig_tiles = perf.period_tiles

        def _tiles_with_fixed_now(*args, **kwargs):
            kwargs["now"] = _FRIDAY_POST_CLOSE
            return _orig_tiles(*args, **kwargs)

        monkeypatch.setattr("api.routers.portfolios.performance.period_tiles", _tiles_with_fixed_now)

        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        puid = uuid.UUID(pid)
        _snap(db_session, tenant, puid, date(2024, 5, 29), 1000.0)
        _snap(db_session, tenant, puid, session_day, 1000.0)
        _bars(db_session, "QQQ", [(date(2024, 6, 26), 300.0)])

        r = client.get(f"/portfolios/{pid}/performance/tiles", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        today_tile = next(t for t in r.json()["tiles"] if t["period"] == "today")
        qqq = next(b for b in today_tile["benchmarks"] if b["symbol"] == "QQQ")
        assert qqq["ret"] == pytest.approx(-0.05)
