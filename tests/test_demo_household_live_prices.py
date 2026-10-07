"""Demo household live pricing — each ``DEMO-<SYM>`` follows ``<SYM>``'s real daily closes
after the fixture's last month, and is rated as ``<SYM>``.

Before this, nothing priced a ``DEMO-`` symbol after the fixture's final month: Glance
read "as of" that month indefinitely, every later daily snapshot carried the same NAV,
and the Market board's Held scope was entirely unrated because ratings are keyed by
real symbols. These tests pin the three properties the demo depends on:

  * a ``DEMO-`` close on a post-anchor date D moves from the anchor exactly as the real
    symbol moved, so its day-over-day return on D IS the real symbol's return on D, and
    nothing is ever written under the real symbol;
  * NAV snapshots recorded while the prices were frozen are restated, so the first live
    day's TODAY tile is one day's move, not the whole move since the anchor;
  * every rating lookup resolves a household symbol through its real listing.
"""

from __future__ import annotations

import csv
import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from api.config import settings
from api.db import models
from api.routers import market_board
from api.services import analytics, demo_household, technical_rating
from api.services import performance as perf
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint

DEMO_HEADERS = {"X-Tenant-Id": str(demo_household.DEMO_TENANT_ID)}
PID = demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID


def _weekdays_after(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def _real_series(anchor: date, sessions: list[date], *, base: float, step: float) -> list[ClosePoint]:
    """A real symbol's closes: one on the last weekday on/before ``anchor`` (the chain
    base — the fixture's anchor is a calendar date, 2026-08-15 was a Saturday), then a
    deterministic, non-constant walk over ``sessions``."""
    pre = anchor
    while pre.weekday() >= 5:
        pre -= timedelta(days=1)
    points = [ClosePoint(bar_date=pre - timedelta(days=1), close=base * 0.97), ClosePoint(bar_date=pre, close=base)]
    for i, d in enumerate(sessions, start=1):
        points.append(ClosePoint(bar_date=d, close=round(base * (1 + step * i + (0.004 if i % 2 else -0.003)), 4)))
    return points


def _source(series_by_real: dict[str, list[ClosePoint]]):
    calls = []

    def _src(symbols, start, end):
        calls.append((tuple(symbols), start, end))
        return {
            s: [p for p in series_by_real[s] if start <= p.bar_date <= end]
            for s in symbols
            if s in series_by_real
        }

    _src.calls = calls
    return _src


def _bars(session, symbol: str) -> dict[date, float]:
    return {
        p.bar_date: p.close
        for p in price_service.close_history_by_symbol(session, [symbol]).get(symbol, [])
    }


# ── The fixture shape the restatement relies on ─────────────────────────────────


def test_fixture_has_no_activity_after_its_last_close():
    """``_restate_post_anchor_snapshots`` values every post-anchor date at TODAY's
    quantities. That is only exact while no fixture activity is dated after the anchor."""
    anchor, _ = demo_household.fixture_anchor()
    path = os.path.join(demo_household._FIXTURE_DIR, "transactions.csv")
    with open(path) as f:
        last_activity = max(date.fromisoformat(r["date"]) for r in csv.DictReader(f))
    assert last_activity <= anchor


def test_reference_symbols_cover_the_household_and_nothing_else():
    assert set(demo_household.REFERENCE_SYMBOLS) == set(demo_household.SECURITY_META)
    assert demo_household.reference_symbol("DEMO-AAPL") == "AAPL"
    assert demo_household.reference_symbol("DEMO-VTI") == "VTI"
    # A real tenant's ticker and a Showcase-only fixture symbol pass through unchanged.
    assert demo_household.reference_symbol("AAPL") == "AAPL"
    assert demo_household.reference_symbol("DEMO-MMF") == "DEMO-MMF"
    assert demo_household.reference_symbol("DEMO-UST-2026") == "DEMO-UST-2026"


# ── chained_closes (pure) ───────────────────────────────────────────────────────


def test_chained_close_moves_exactly_as_the_real_symbol_moves():
    anchor = date(2026, 8, 15)
    sessions = _weekdays_after(anchor, 6)
    real = {"AAPL": _real_series(anchor, sessions, base=305.93, step=0.01)}
    chained, unpriced = demo_household.chained_closes(anchor, {"DEMO-AAPL": 257.05, "DEMO-VTI": 330.97}, real)

    assert unpriced == ["DEMO-VTI"]  # the spine has no VTI: held, never substituted
    pts = chained["DEMO-AAPL"]
    assert [p.bar_date for p in pts] == sessions  # only sessions AFTER the anchor
    real_by_date = {p.bar_date: p.close for p in real["AAPL"]}
    base = real_by_date[date(2026, 8, 14)]
    for p in pts:
        assert p.close == pytest.approx(257.05 * real_by_date[p.bar_date] / base, rel=1e-8)  # stored to 6 dp
    # Day-over-day: the DEMO return on each session equals the real return that session.
    for prev, cur in zip(pts, pts[1:], strict=False):
        real_ret = real_by_date[cur.bar_date] / real_by_date[prev.bar_date] - 1
        assert cur.close / prev.close - 1 == pytest.approx(real_ret, abs=1e-7)


def test_chained_close_needs_a_real_close_on_or_before_the_anchor():
    anchor = date(2026, 8, 15)
    late_only = [ClosePoint(bar_date=date(2026, 8, 20), close=100.0)]
    chained, unpriced = demo_household.chained_closes(anchor, {"DEMO-KO": 67.97}, {"KO": late_only})
    assert chained == {}
    assert unpriced == ["DEMO-KO"]


# ── refresh_live_prices against a seeded household ─────────────────────────────


@pytest.fixture()
def seeded(db_session):
    demo_household.ensure_demo_household_seeded(db_session)
    return db_session


def test_refresh_is_a_noop_without_the_household(db_session):
    src = _source({})
    result = demo_household.refresh_live_prices(db_session, today=date(2026, 10, 5), source=src)
    assert result.bars_written == 0 and result.anchor is None
    assert src.calls == []  # no spine read for a deployment that never seeded it


def test_demo_price_on_date_d_follows_the_real_close_on_d(seeded):
    session = seeded
    anchor, anchor_closes = demo_household.fixture_anchor()
    sessions = _weekdays_after(anchor, 8)
    today = sessions[-1]
    real = {
        "AAPL": _real_series(anchor, sessions, base=305.93, step=0.01),
        "NVDA": _real_series(anchor, sessions, base=224.91, step=-0.004),
        "VOO": _real_series(anchor, sessions, base=711.78, step=0.002),
    }
    src = _source(real)

    result = demo_household.refresh_live_prices(session, today=today, source=src)

    assert result.anchor == anchor
    assert set(result.priced) == {"DEMO-AAPL", "DEMO-NVDA", "DEMO-VOO"}
    assert "DEMO-VTI" in result.unpriced and "DEMO-BND" in result.unpriced
    assert result.bars_written == 3 * len(sessions)
    # The spine was asked for REAL symbols only — never a DEMO- one.
    (asked, _start, end), = src.calls
    assert end == today and not any(s.startswith("DEMO-") for s in asked)
    assert set(asked) == set(demo_household.REFERENCE_SYMBOLS.values())

    for demo, real_sym in (("DEMO-AAPL", "AAPL"), ("DEMO-NVDA", "NVDA"), ("DEMO-VOO", "VOO")):
        bars = _bars(session, demo)
        real_by_date = {p.bar_date: p.close for p in real[real_sym]}
        base = real_by_date[max(d for d in real_by_date if d <= anchor)]
        assert bars[anchor] == pytest.approx(anchor_closes[demo])  # the fixture is untouched
        for d in sessions:
            assert bars[d] == pytest.approx(anchor_closes[demo] * real_by_date[d] / base, rel=1e-6)
        d1, d0 = sessions[-1], sessions[-2]
        assert bars[d1] / bars[d0] == pytest.approx(real_by_date[d1] / real_by_date[d0], abs=1e-6)

    # An unpriced symbol keeps its last fixture close — no bar after the anchor.
    assert max(_bars(session, "DEMO-VTI")) == anchor
    # Nothing was written under a real symbol: no Security row exists for AAPL/NVDA/VOO.
    real_rows = session.scalars(
        select(models.Security.symbol).where(models.Security.symbol.in_(["AAPL", "NVDA", "VOO"]))
    ).all()
    assert real_rows == []


def test_refresh_is_idempotent(seeded):
    session = seeded
    anchor, _ = demo_household.fixture_anchor()
    sessions = _weekdays_after(anchor, 4)
    src = _source({"AAPL": _real_series(anchor, sessions, base=305.93, step=0.01)})
    first = demo_household.refresh_live_prices(session, today=sessions[-1], source=src)
    second = demo_household.refresh_live_prices(session, today=sessions[-1], source=src)
    assert first.bars_written == len(sessions)
    assert second.bars_written == 0 and second.snapshots_restated == 0


def test_frozen_snapshots_are_restated_so_today_is_one_days_move(seeded):
    """The failure this prevents: daily snapshots recorded at the frozen anchor prices,
    then one live snapshot, would put the whole move since the anchor into one day."""
    session = seeded
    tid = demo_household.DEMO_TENANT_ID
    anchor, _ = demo_household.fixture_anchor()
    sessions = _weekdays_after(anchor, 5)
    # What production has today: daily snapshots recorded while prices were frozen.
    for d in sessions[:-1]:
        perf.record_snapshot(session, tid, PID, today=d, source=lambda s: {})
        perf.record_account_snapshots(session, tid, PID, today=d, source=lambda s: {})
    frozen = {
        r.snap_date: float(r.nav)
        for r in session.scalars(
            select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == PID, models.NavSnapshot.snap_date > anchor)
        )
    }
    assert len(set(frozen.values())) == 1  # flat: the bug as the demo walk observed it

    real = {
        sym: _real_series(anchor, sessions, base=100.0, step=0.01)
        for sym in demo_household.REFERENCE_SYMBOLS.values()
        if sym not in ("VTI", "BND")
    }
    result = demo_household.refresh_live_prices(session, today=sessions[-1], source=_source(real))
    assert result.snapshots_restated >= len(sessions) - 1

    # The live snapshot for the last session, recorded the way daily-refresh records it.
    perf.record_snapshot(session, tid, PID, today=sessions[-1], source=lambda s: {})
    navs = {
        r.snap_date: float(r.nav)
        for r in session.scalars(
            select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == PID, models.NavSnapshot.snap_date > anchor)
        )
    }
    # Each restated date equals today's quantities at that date's chained closes...
    held = [h for h in analytics.valued_holdings(session, tid, PID) if h.quantity > 0]
    for d in sessions:
        expected = 0.0
        for h in held:
            bars = _bars(session, h.ticker)
            on = max(b for b in bars if b <= d)
            expected += h.quantity * bars[on]
        assert navs[d] == pytest.approx(expected, rel=1e-9)
    # ...so the last day's move is that day's move alone, not the move since the anchor.
    last, prev = sessions[-1], sessions[-2]
    day_move = navs[last] / navs[prev] - 1
    since_anchor = navs[last] / frozen[prev] - 1
    assert navs[prev] != pytest.approx(frozen[prev])  # the frozen value is gone
    assert abs(day_move) < abs(since_anchor)
    # Per-account snapshots are restated at the same closes and still sum to the portfolio.
    acct = session.scalars(
        select(models.AccountNavSnapshot).where(
            models.AccountNavSnapshot.portfolio_id == PID, models.AccountNavSnapshot.snap_date == prev
        )
    ).all()
    assert len(acct) == 3
    assert sum(float(a.nav) for a in acct) == pytest.approx(navs[prev], rel=1e-9)
    # The composition legs the Glance movers read were rewritten at the same closes.
    row = session.scalars(
        select(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == PID, models.NavSnapshot.snap_date == prev)
    ).one()
    assert sum(leg["value"] for leg in row.composition["legs"]) == pytest.approx(navs[prev], rel=1e-9)
    assert {leg["price_date"] for leg in row.composition["legs"]} >= {prev.isoformat()}
    # A second pass finds nothing left to restate.
    again = demo_household.refresh_live_prices(session, today=sessions[-1], source=_source(real))
    assert again.bars_written == 0 and again.snapshots_restated == 0


def test_refresh_before_the_anchor_writes_nothing(seeded):
    anchor, _ = demo_household.fixture_anchor()
    src = _source({"AAPL": _real_series(anchor, _weekdays_after(anchor, 2), base=305.93, step=0.01)})
    result = demo_household.refresh_live_prices(seeded, today=anchor, source=src)
    assert result.anchor == anchor and result.bars_written == 0
    assert src.calls == []


# ── Ratings resolve through the real symbol ──────────────────────────────────────


def _rating_art() -> dict:
    return {
        "schema_version": 1,
        "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ratings": {
            "AAPL": {"score": 0.4, "label": "Buy", "ma_score": 0.5, "osc_score": 0.3,
                     "n_buy": 8, "n_neutral": 2, "n_sell": 1, "n_votes": 11},
            "NVDA": {"score": -0.6, "label": "Sell", "ma_score": -0.5, "osc_score": -0.7,
                     "n_buy": 1, "n_neutral": 1, "n_sell": 9, "n_votes": 11},
        },
    }


def test_market_board_rates_household_rows_by_their_real_symbol(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "tier_simulator", False)
    monkeypatch.setattr(settings, "feed_entitled", True)
    real_load = technical_rating.load_technical_rating
    monkeypatch.setattr(
        market_board.technical_rating_service, "load_technical_rating",
        lambda: real_load(intraday_reader=_rating_art),
    )
    # Two live sessions ending today, so the board's 1d change is a real one.
    anchor, _ = demo_household.fixture_anchor()
    today = datetime.now(UTC).date()
    sessions = [d for d in _weekdays_after(anchor, 400) if d <= today][-6:]
    real = {"AAPL": _real_series(anchor, sessions, base=305.93, step=0.01)}
    # The chain base must sit on/before the anchor; the walk covers only the last sessions.
    demo_household.refresh_live_prices(seeded, today=today, source=_source(real))

    r = client.get(f"/portfolios/{PID}/market-board", params={"scope": "held"}, headers=DEMO_HEADERS)
    assert r.status_code == 200
    rows = {row["symbol"]: row for row in r.json()["rows"]}
    assert rows["DEMO-AAPL"]["label"] == "Buy" and rows["DEMO-AAPL"]["score"] == 0.4
    assert rows["DEMO-NVDA"]["label"] == "Sell"
    assert rows["DEMO-AAPL"]["as_of"] is not None
    assert rows["DEMO-KO"]["label"] is None  # KO is not in this artifact: unrated, not guessed
    real_by_date = {p.bar_date: p.close for p in real["AAPL"]}
    expected_1d = real_by_date[sessions[-1]] / real_by_date[sessions[-2]] - 1
    assert rows["DEMO-AAPL"]["change_1d_pct"] == pytest.approx(expected_1d, abs=1e-6)


# ── Wired into daily-refresh ─────────────────────────────────────────────────────


def _spy(symbols, *, source=None):
    return {"SPY": ClosePoint(bar_date=date(2026, 10, 5), close=774.83)} if "SPY" in symbols else {}


def test_daily_refresh_runs_live_pricing_for_its_session_date(db_session, monkeypatch):
    from api import maintenance

    calls = []

    def _fake(session, *, today, source=None):
        calls.append(today)
        return demo_household.LivePriceResult(anchor=date(2026, 8, 15), bars_written=7, priced=["DEMO-AAPL"])

    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy)
    monkeypatch.setattr(demo_household, "refresh_live_prices", _fake)
    result = maintenance.daily_refresh(db_session, today=date(2026, 10, 5))
    assert calls == [date(2026, 10, 5)]
    assert result.demo_household_bars == 7


def test_daily_refresh_survives_a_live_pricing_failure(db_session, monkeypatch):
    from api import maintenance

    def _boom(session, *, today, source=None):
        raise RuntimeError("spine unreachable")

    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy)
    monkeypatch.setattr(demo_household, "refresh_live_prices", _boom)
    result = maintenance.daily_refresh(db_session, today=date(2026, 10, 5))
    assert result.demo_household_bars == 0


def test_daily_refresh_skips_live_pricing_when_demo_is_disabled(db_session, monkeypatch):
    from api import maintenance

    def _never(*a, **k):
        raise AssertionError("live pricing must not run with demo disabled")

    monkeypatch.setattr(settings, "demo_enabled", False)
    monkeypatch.setattr("api.maintenance.fetch_latest_closes", _spy)
    monkeypatch.setattr(demo_household, "refresh_live_prices", _never)
    assert maintenance.daily_refresh(db_session, today=date(2026, 10, 5)).demo_household_bars == 0
