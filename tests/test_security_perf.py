"""Per-security period returns for the Holdings table (metron-ops#87): YTD/LTM from cached
daily closes, Day legs (overnight/intraday/day) from the intraday spine."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from api.db import models
from api.services import analytics, security_perf

_AS_OF_ART = "2026-06-12T15:00:00Z"
_NOW = datetime(2026, 6, 12, 15, 3, tzinfo=UTC)  # 3 min after the write — fresh
_TODAY = date(2026, 6, 12)


def _art(quotes: dict) -> dict:
    return {"schema_version": 1, "as_of_utc": _AS_OF_ART, "source": "yfinance_delayed", "quotes": quotes}


def _seed(session, closes: list[tuple[date, float]]):
    """One USD holding (AAPL, 10 sh) + the given cached close bars."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    # The intraday overlay (and its day-leg decomposition) is opt-in — enable it so these
    # feed-path tests exercise the overlay rather than the default-off gate.
    session.add(models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=pf.id, intraday_enabled=True))
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="A1", currency="USD")
    sec = models.Security(symbol="AAPL", yf_symbol="AAPL", currency="USD")
    session.add_all([acct, sec])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type="BUY", quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 1), source_key="buy-1",
        )
    )
    for d, c in closes:
        session.add(models.PriceBar(security_id=sec.id, bar_date=d, close=c, currency="USD"))
    session.commit()
    return tenant.id, pf.id


# History spans >1y: pre-year-start + year-start + as-of, so both windows resolve.
_CLOSES = [
    (date(2024, 1, 2), 100.0),
    (date(2024, 6, 3), 120.0),
    (date(2025, 1, 2), 150.0),
    (date(2025, 6, 1), 180.0),
]

_PERF_ART = {
    "as_of": "2025-06-01",
    "performance": {
        "AAPL": {"ytd_pct": 0.20, "ltm_pct": 0.50},
    },
}


def test_ytd_and_ltm_from_spine(db_session):
    tid, pid = _seed(db_session, _CLOSES)
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2025, 6, 1), feed_entitled=True,
        performance_reader=lambda: _PERF_ART,
    )
    sr = out["AAPL"]
    assert sr.ytd_pct == pytest.approx(0.20)
    assert sr.ltm_pct == pytest.approx(0.50)
    assert sr.day_pct is None


def test_ytd_and_ltm_blank_off_feed(db_session):
    tid, pid = _seed(db_session, _CLOSES)
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2025, 6, 1), feed_entitled=False,
        performance_reader=lambda: _PERF_ART,
    )
    sr = out["AAPL"]
    assert sr.ytd_pct is None
    assert sr.ltm_pct is None


def test_window_omitted_when_history_too_short(db_session):
    tid, pid = _seed(db_session, [(date(2025, 5, 1), 170.0), (date(2025, 6, 1), 180.0)])
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2025, 6, 1), feed_entitled=True,
        performance_reader=lambda: {"performance": {}},
    )
    sr = out["AAPL"]
    assert sr.ytd_pct is None
    assert sr.ltm_pct is None


def test_day_legs_from_intraday_feed(db_session):
    tid, pid = _seed(db_session, [(date(2026, 6, 11), 100.0)])
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=_TODAY, feed_entitled=True, now=_NOW,
        reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}}),
    )
    sr = out["AAPL"]
    assert sr.overnight_pct == pytest.approx(0.10)   # (110−100)/100
    assert sr.intraday_pct == pytest.approx(0.181818, rel=1e-3)  # (130−110)/110
    assert sr.day_pct == pytest.approx(0.30)         # (130−100)/100


def test_enrich_holdings_populates_holding_fields(db_session):
    tid, pid = _seed(db_session, _CLOSES)
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(
        db_session, tid, pid, held, as_of=date(2025, 6, 1), feed_entitled=True,
        performance_reader=lambda: _PERF_ART,
    )
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.ytd_pct == pytest.approx(0.20)
    assert h.ltm_pct == pytest.approx(0.50)


def test_enrich_holdings_day_change_dollars(db_session):
    """day_change = market_value × day_pct/(1+day_pct) — the base-currency $ the position
    moved today (price leg only). Live-gated exactly like day_pct."""
    tid, pid = _seed(db_session, [(date(2026, 6, 11), 100.0)])
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(
        db_session, tid, pid, held, as_of=_TODAY, feed_entitled=True, now=_NOW,
        reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}}),
    )
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.day_pct == pytest.approx(0.30)
    # valued at the settled close here (prices override not passed), so market_value is
    # qty × 100; the backed-out prior-close value is mv/(1.30) → day_change = mv × 0.3/1.3.
    assert h.day_change == pytest.approx(h.market_value * 0.30 / 1.30)


def test_enrich_holdings_day_change_none_when_settled(db_session):
    """No day leg (settled regime / day_legs=False) → day_change stays None, never 0."""
    tid, pid = _seed(db_session, [(date(2026, 6, 11), 100.0)])
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(
        db_session, tid, pid, held, as_of=_TODAY, feed_entitled=True, now=_NOW, day_legs=False,
    )
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.day_pct is None and h.day_change is None


@pytest.mark.parametrize(
    "price_date,today,expected",
    [
        (date(2026, 6, 25), date(2026, 6, 25), 0),   # priced today
        (date(2026, 6, 26), date(2026, 6, 25), 0),   # future bar → never stale
        (date(2026, 6, 24), date(2026, 6, 25), 1),   # prior session — normal pre-close
        (date(2026, 6, 23), date(2026, 6, 25), 2),   # one full session skipped → stale
        (date(2026, 6, 19), date(2026, 6, 22), 1),   # Fri close read Mon — weekend skipped
        (date(2026, 6, 19), date(2026, 6, 23), 2),   # Fri close still unrefreshed Tue → stale
        # 2026-07-03 is an NYSE holiday (Independence Day observed, 7/4 fell on a Saturday).
        (date(2026, 7, 2), date(2026, 7, 6), 1),      # Thu close read Mon after the holiday — fresh
        (date(2026, 7, 2), date(2026, 7, 7), 2),      # Thu close still unrefreshed Tue → stale
    ],
)
def test_sessions_behind(price_date, today, expected):
    assert security_perf.sessions_behind(price_date, today) == expected


# 2026-06-25 14:00 UTC = 10:00 ET on 2026-06-25 (market open).
_NOW_0625 = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)


def test_enrich_flags_stale_close_fed_price(db_session):
    # Latest cached close is 2026-06-23 while "today" is 2026-06-25 → a full session was
    # skipped → the close-fed price is flagged stale (the RKLB-95.12 failure mode).
    tid, pid = _seed(db_session, [(date(2026, 6, 23), 95.12)])
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(db_session, tid, pid, held, as_of=date(2026, 6, 25), feed_entitled=False, now=_NOW_0625)
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.last_price_from_close is True
    assert h.last_price_stale is True


def test_enrich_does_not_flag_fresh_close(db_session):
    tid, pid = _seed(db_session, [(date(2026, 6, 24), 100.0)])
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(db_session, tid, pid, held, as_of=date(2026, 6, 25), feed_entitled=False, now=_NOW_0625)
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.last_price_stale is False  # 1 session behind = normal before today's close prints


def test_enrich_does_not_flag_broker_snapshot(db_session):
    # A broker-statement snapshot is legitimately old; it must NOT read as a stalled live
    # feed even when its as-of date is weeks back.
    tid, pid = _seed(db_session, _CLOSES)
    broker = analytics.Holding(
        ticker="BNDX", quantity=1, avg_cost=50.0, cost_basis=50.0,
        last_price=50.0, last_price_date=date(2026, 5, 1), last_price_from_close=False,
    )
    security_perf.enrich_holdings(db_session, tid, pid, [broker], as_of=date(2026, 6, 25), feed_entitled=False, now=_NOW_0625)
    assert broker.last_price_stale is False


def test_enrich_flags_stale_positions(db_session):
    # A broker-snapshot holding whose as_of is 2+ trading sessions behind "today" means
    # the daily re-sync hasn't run recently — the metron-ops#150 failure mode (a sold
    # PLTR position still showing its pre-sale share count).
    tid, pid = _seed(db_session, _CLOSES)
    broker = analytics.Holding(
        ticker="PLTR", quantity=100, avg_cost=20.0, cost_basis=2000.0,
        broker_as_of=date(2026, 6, 22),
    )
    security_perf.enrich_holdings(db_session, tid, pid, [broker], as_of=date(2026, 6, 25), feed_entitled=False, now=_NOW_0625)
    assert broker.positions_stale is True


def test_enrich_does_not_flag_fresh_positions(db_session):
    tid, pid = _seed(db_session, _CLOSES)
    broker = analytics.Holding(
        ticker="PLTR", quantity=100, avg_cost=20.0, cost_basis=2000.0,
        broker_as_of=date(2026, 6, 24),
    )
    security_perf.enrich_holdings(db_session, tid, pid, [broker], as_of=date(2026, 6, 25), feed_entitled=False, now=_NOW_0625)
    assert broker.positions_stale is False  # 1 session behind = normal before a sync fires


def test_enrich_does_not_flag_ledger_only_holdings(db_session):
    # A CSV/OFX ledger-derived holding has no broker snapshot to go stale — broker_as_of
    # stays None, so positions_stale must stay False regardless of "today".
    tid, pid = _seed(db_session, _CLOSES)
    held = analytics.valued_holdings(db_session, tid, pid)
    security_perf.enrich_holdings(db_session, tid, pid, held, as_of=date(2025, 6, 1), feed_entitled=True, performance_reader=lambda: _PERF_ART)
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.broker_as_of is None
    assert h.positions_stale is False


def test_ltm_from_spine_artifact(db_session):
    """Holdings LTM reads the security_performance spine — not local price_bars."""
    tid, pid = _seed(db_session, [(date(2025, 7, 6), 518.0), (date(2026, 7, 2), 193.98)])
    ltm = 193.98 / 126.36 - 1.0
    perf_art = {"performance": {"AAPL": {"ltm_pct": ltm, "ytd_pct": ltm}}}
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2026, 7, 6), feed_entitled=True,
        performance_reader=lambda: perf_art,
    )
    assert out["AAPL"].ltm_pct == pytest.approx(ltm, rel=1e-3)
