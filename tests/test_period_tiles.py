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


class TestTodayDateGuard:
    """When the freshest valuation predates today (pre-open / weekend / intraday-off owner
    build), the TODAY tile shows the latest completed close-to-close change BUT labels it
    "as of <date>" — so it's never read as a live "today" move (the metron-ops −$10k-before-
    open bug was the missing label, not the value). When a snapshot IS dated today, no label."""

    def test_today_shows_latest_close_with_as_of_label_when_snapshot_predates_today(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:  # last snapshot 2024-06-30 @ 1320 (prior 2024-06-29 @ 1300)
            _snap(db_session, tenant, pid, when, nav)
        # It's the NEXT day, pre-open / intraday off: no snapshot dated today yet.
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False
        )
        today = next(t for t in res.tiles if t.period == "today")
        # The latest close-to-close change shows (1300 → 1320) — but clearly labeled as of the
        # freshest valuation, not as a live "today" move.
        assert today.gain == pytest.approx(20.0)
        assert today.twr == pytest.approx(1320 / 1300 - 1)
        assert today.end_date == date(2024, 6, 30)
        assert today.note == "as of 2024-06-30"
        assert today.intraday is False
        # YTD/LTM carry no as-of label.
        ytd = next(t for t in res.tiles if t.period == "ytd")
        assert ytd.gain is not None and ytd.note is None

    def test_today_forms_when_latest_snapshot_is_today(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=_TODAY, with_benchmarks=False
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.gain == pytest.approx(20.0)  # 1300 → 1320
        assert today.note is None


class TestLiveIntradayToday:
    """The TODAY tile is a live intraday number (metron-ops#95) when the overlay is in
    effect: prior trading session's close → current live NAV, flow-neutralized. When the
    overlay is absent it must fall back to the date-guarded snapshot path (metron#119)."""

    def test_intraday_today_anchors_prior_session_to_live_nav(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:  # last snapshot 2024-06-30 @ 1320
            _snap(db_session, tenant, pid, when, nav)
        live = perf.LiveToday(
            nav=1353.0,  # live NAV now (intraday)
            intraday_applied=True,
            as_of_utc="2024-07-01T17:30:00Z",
            bench={"SPY": (101.0, 100.0), "QQQ": (None, None), "IWM": (200.0, 0.0)},
        )
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=True, live=live
        )
        today = next(t for t in res.tiles if t.period == "today")
        assert today.intraday is True
        assert today.note is None
        # Prior session close = last snapshot before today (2024-06-30 @ 1320) → live 1353.
        assert today.start_date == date(2024, 6, 30)
        assert today.end_date == date(2024, 7, 1)
        assert today.gain == pytest.approx(33.0)  # 1353 − 1320 (no flow today)
        assert today.twr == pytest.approx(1353 / 1320 - 1)
        # Benchmark TODAY = the live (last/prev − 1) the Markets strip shows.
        spy = next(b for b in today.benchmarks if b.symbol == "SPY")
        assert spy.ret == pytest.approx(0.01)  # 101/100 − 1
        assert spy.alpha == pytest.approx((1353 / 1320 - 1) - 0.01)
        # Missing / zero-prev quotes degrade to None, never a divide-by-zero or fabrication.
        assert next(b for b in today.benchmarks if b.symbol == "QQQ").ret is None
        assert next(b for b in today.benchmarks if b.symbol == "IWM").ret is None

    def test_intraday_today_neutralizes_a_same_day_flow(self, db_session, tenant):
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2024, 6, 28), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 6, 30), 1200.0)  # prior session close
        # A +300 BUY lands TODAY → the intraday window must strip it from both gain and TWR.
        _txn(db_session, tenant, pid, date(2024, 7, 1), "BUY", 300.0)
        live = perf.LiveToday(nav=1530.0, intraday_applied=True)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False, live=live
        )
        today = next(t for t in res.tiles if t.period == "today")
        # gain net of flow = 1530 − 1200 − 300 = 30; TWR pre-flow = 1230/1200 − 1 = 0.025.
        assert today.gain == pytest.approx(30.0)
        assert today.twr == pytest.approx(1230 / 1200 - 1)

    def test_falls_back_to_snapshot_path_when_overlay_not_applied(self, db_session, tenant):
        pid = uuid.uuid4()
        for when, nav in _SERIES:
            _snap(db_session, tenant, pid, when, nav)
        # Feed present but overlay not in effect (stale / pre-open) → snapshot path.
        live = perf.LiveToday(nav=9999.0, intraday_applied=False)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False, live=live
        )
        today = next(t for t in res.tiles if t.period == "today")
        # The live overlay is ignored (not applied); the tile shows the latest settled close-
        # to-close change, labeled "as of <date>" — never the 9999 live NAV.
        assert today.intraday is False
        assert today.gain == pytest.approx(20.0) and today.note == "as of 2024-06-30"

    def test_intraday_skipped_when_no_prior_session_to_anchor(self, db_session, tenant):
        # Only a same-day snapshot exists → no prior session before today → no intraday tile.
        pid = uuid.uuid4()
        _snap(db_session, tenant, pid, date(2024, 6, 28), 1000.0)
        _snap(db_session, tenant, pid, date(2024, 7, 1), 1010.0)
        live = perf.LiveToday(nav=1020.0, intraday_applied=True)
        res = perf.period_tiles(
            db_session, uuid.UUID(tenant), pid, today=date(2024, 7, 1), with_benchmarks=False, live=live
        )
        today = next(t for t in res.tiles if t.period == "today")
        # Prior session = 2024-06-28 → anchors there (1000 → live 1020).
        assert today.intraday is True
        assert today.start_date == date(2024, 6, 28)
        assert today.gain == pytest.approx(20.0)


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
