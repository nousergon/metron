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
    (date(2024, 1, 2), 100.0),   # before LTM start
    (date(2024, 6, 3), 120.0),   # first close on/after LTM start (2025-06-12)? no — see below
    (date(2025, 1, 2), 150.0),   # first close on/after YTD start (2025-01-01) for as_of 2025
    (date(2025, 6, 1), 180.0),
]


def test_ytd_and_ltm_from_cached_closes(db_session):
    tid, pid = _seed(db_session, _CLOSES)
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2025, 6, 1), feed_entitled=False
    )
    sr = out["AAPL"]
    # YTD: first close on/after 2025-01-01 is 150 (2025-01-02); latest 180 → +20%.
    assert sr.ytd_pct == pytest.approx(0.20)
    # LTM: first close on/after 2024-06-01 is 120 (2024-06-03); latest 180 → +50%.
    assert sr.ltm_pct == pytest.approx(0.50)
    # No feed → no day legs.
    assert sr.day_pct is None and sr.overnight_pct is None and sr.intraday_pct is None


def test_window_omitted_when_history_too_short(db_session):
    # Only recent bars — history does not reach back to the YTD/LTM window start.
    tid, pid = _seed(db_session, [(date(2025, 5, 1), 170.0), (date(2025, 6, 1), 180.0)])
    out = security_perf.per_security_returns(
        db_session, tid, pid, ["AAPL"], as_of=date(2025, 6, 1), feed_entitled=False
    )
    sr = out["AAPL"]
    assert sr.ytd_pct is None  # no bar on/before 2025-01-01
    assert sr.ltm_pct is None  # no bar on/before 2024-06-01


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
    security_perf.enrich_holdings(db_session, tid, pid, held, as_of=date(2025, 6, 1), feed_entitled=False)
    h = next(h for h in held if h.ticker == "AAPL")
    assert h.ytd_pct == pytest.approx(0.20)
    assert h.ltm_pct == pytest.approx(0.50)
