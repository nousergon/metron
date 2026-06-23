"""Daily overnight/intraday/day decomposition history (metron-ops#87): record from the
intraday spine, accrue forward, expose the cumulative split."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from api.db import models
from api.services import performance

_AS_OF = "2026-06-12T15:00:00Z"
_NOW = datetime(2026, 6, 12, 15, 3, tzinfo=UTC)  # fresh vs the write


def _art(quotes: dict) -> dict:
    return {"schema_version": 1, "as_of_utc": _AS_OF, "source": "yfinance_delayed", "quotes": quotes}


def _seed(session):
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
            trade_date=date(2024, 1, 1), source_key="b1",
        )
    )
    session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 6, 11), close=120.0, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def _reader():
    return _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}})


def test_record_then_history_cumulative(db_session):
    tid, pid = _seed(db_session)
    # Portfolio legs are gain / prior-close MV, so overnight + intraday = day (additive):
    # prev-MV 10×100=1000; overnight gain 10×(110−100)=100 → 0.10; intraday 10×(130−110)=200 → 0.20.
    row = performance.record_intraday_legs(
        db_session, tid, pid, today=date(2026, 6, 12), feed_entitled=True, now=_NOW, reader=_reader
    )
    assert row is not None
    assert float(row.overnight_pct) == pytest.approx(0.10)
    assert float(row.intraday_pct) == pytest.approx(0.20)
    assert float(row.day_pct) == pytest.approx(0.30)

    # Idempotent per (portfolio, day).
    performance.record_intraday_legs(
        db_session, tid, pid, today=date(2026, 6, 12), feed_entitled=True, now=_NOW, reader=_reader
    )
    hist = performance.intraday_leg_history(db_session, tid, pid)
    assert hist.n_days == 1
    assert hist.cum_overnight_pct == pytest.approx(0.10)
    assert hist.cum_intraday_pct == pytest.approx(0.20)
    assert hist.cum_day_pct == pytest.approx(0.30)


def _reader_day2():
    # Day 2: overnight +0% (open == prev), intraday +10% (last 110 vs open 100, prev-MV 1000).
    return {
        "schema_version": 1, "as_of_utc": "2026-06-13T15:00:00Z", "source": "yfinance_delayed",
        "quotes": {"AAPL": {"prev_close": 100.0, "open": 100.0, "last": 110.0}},
    }


def test_two_days_compound(db_session):
    tid, pid = _seed(db_session)
    performance.record_intraday_legs(db_session, tid, pid, today=date(2026, 6, 12), feed_entitled=True, now=_NOW, reader=_reader)
    now2 = datetime(2026, 6, 13, 15, 3, tzinfo=UTC)
    performance.record_intraday_legs(db_session, tid, pid, today=date(2026, 6, 13), feed_entitled=True, now=now2, reader=_reader_day2)
    hist = performance.intraday_leg_history(db_session, tid, pid)
    assert hist.n_days == 2
    # Overnight compounds 1.10 × 1.00 − 1 = 0.10; intraday 1.20 × 1.10 − 1 = 0.32.
    assert hist.cum_overnight_pct == pytest.approx(0.10)
    assert hist.cum_intraday_pct == pytest.approx(0.32)


def test_skipped_without_feed(db_session):
    tid, pid = _seed(db_session)
    assert performance.record_intraday_legs(
        db_session, tid, pid, today=date(2026, 6, 12), feed_entitled=False, now=_NOW, reader=_reader
    ) is None
    assert performance.intraday_leg_history(db_session, tid, pid).n_days == 0
