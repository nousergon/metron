"""Realized gains + income converted to the base currency at the FX rate AS OF the
event date (close date for a lot, trade date for a dividend) — not today's rate."""

from __future__ import annotations

from datetime import date

import pytest

from api.db import models
from api.services import analytics, fx
from portfolio_analytics.prices import ClosePoint


def _txn(account_id, tenant_id, security_id, *, typ, when, qty=0.0, price=0.0, amount=0.0, ccy="HKD", key):
    return models.Transaction(
        tenant_id=tenant_id, account_id=account_id, security_id=security_id, txn_type=typ,
        quantity=qty, price=price, amount=amount, currency=ccy, trade_date=when, source_key=key,
    )


@pytest.fixture()
def hk_world(db_session):
    """A USD portfolio with a HKD 1299 round-trip (BUY then SELL → realized gain) plus a
    HKD dividend, and cached FX rates around each event date."""
    s = db_session
    t = models.Tenant(name="t")
    s.add(t)
    s.flush()
    p = models.Portfolio(tenant_id=t.id, name="p", base_currency="USD")
    s.add(p)
    s.flush()
    acct = models.Account(tenant_id=t.id, portfolio_id=p.id, broker="ibkr_flex", external_id="U1", currency="USD")
    s.add(acct)
    sec = models.Security(symbol="1299", currency="HKD", exchange="SEHK", yf_symbol="1299.HK")
    s.add(sec)
    s.flush()
    s.add_all([
        _txn(acct.id, t.id, sec.id, typ="BUY", when=date(2024, 1, 10), qty=100, price=60, key="b1"),
        _txn(acct.id, t.id, sec.id, typ="DIVIDEND", when=date(2024, 3, 1), amount=50, key="d1"),
        _txn(acct.id, t.id, sec.id, typ="SELL", when=date(2024, 6, 10), qty=100, price=80, key="s1"),
    ])
    # As-of rates: dividend reads 2024-02-28 (carry-forward to 03-01); SELL reads 2024-06-07.
    s.add_all([
        models.FxRate(currency="HKD", base="USD", rate_date=date(2024, 2, 28), rate=0.127),
        models.FxRate(currency="HKD", base="USD", rate_date=date(2024, 6, 7), rate=0.130),
    ])
    s.commit()
    return t.id, p.id


class TestRealizedFx:
    def test_realized_converts_at_close_date_rate(self, db_session, hk_world):
        tid, pid = hk_world
        lots = analytics.realized(db_session, tid, pid)
        assert len(lots) == 1
        lot = lots[0]
        assert lot.currency == "HKD"
        assert lot.gain == pytest.approx(2000.0)  # native: 100*(80-60)
        assert lot.fx_rate == pytest.approx(0.130)  # as of 2024-06-10 → carry 06-07
        assert lot.gain_base == pytest.approx(260.0)  # 2000 * 0.130
        assert lot.proceeds_base == pytest.approx(8000 * 0.130)
        assert not lot.long_term  # Jan→Jun < 1yr

    def test_income_converts_each_component_at_its_date(self, db_session, hk_world):
        tid, pid = hk_world
        rows = {r.year: r for r in analytics.income(db_session, tid, pid)}
        y = rows[2024]
        assert y.realized_st == pytest.approx(260.0)  # 2000 HKD * 0.130 (close-date)
        assert y.dividends == pytest.approx(50 * 0.127)  # pay-date rate, carry-forward
        assert y.realized_lt == 0.0

    def test_missing_rate_excludes_from_base(self, db_session, hk_world):
        # Wipe ALL HKD rates → nothing on/before any event date; no fabrication.
        db_session.query(models.FxRate).delete()
        db_session.commit()
        lot = analytics.realized(db_session, *hk_world)[0]
        assert lot.gain == pytest.approx(2000.0)  # native still shown
        assert lot.gain_base is None and lot.fx_rate is None  # but no base value fabricated
        # Both income components are unconvertible → no 2024 income row at all.
        assert analytics.income(db_session, *hk_world) == []

    def test_carry_forward_uses_nearest_earlier_rate(self, db_session, hk_world):
        # Drop only the 06-07 rate → the SELL (06-10) carries forward to the 02-28 rate.
        db_session.query(models.FxRate).filter(models.FxRate.rate_date == date(2024, 6, 7)).delete()
        db_session.commit()
        lot = analytics.realized(db_session, *hk_world)[0]
        assert lot.fx_rate == pytest.approx(0.127)  # nearest earlier
        assert lot.gain_base == pytest.approx(2000 * 0.127)


class TestForeignTransactionSpan:
    def test_span(self, db_session, hk_world):
        tid, pid = hk_world
        ccys, earliest = analytics.foreign_transaction_currencies(db_session, tid, pid)
        assert ccys == ["HKD"]
        assert earliest == date(2024, 1, 10)


class TestRateAsOfAndBackfill:
    def test_rate_as_of_carry_forward(self, db_session):
        db_session.add(models.FxRate(currency="HKD", base="USD", rate_date=date(2024, 6, 7), rate=0.13))
        db_session.commit()
        assert fx.rate_as_of(db_session, "HKD", date(2024, 6, 10)) == pytest.approx(0.13)  # carry fwd
        assert fx.rate_as_of(db_session, "HKD", date(2024, 6, 5)) is None  # nothing on/before
        assert fx.rate_as_of(db_session, "USD", date(2024, 6, 10)) == 1.0

    def test_backfill_inserts_history(self, db_session):
        def hist(symbols, start, end, *, source=None):
            series = [ClosePoint(date(2024, 1, 2), 0.128), ClosePoint(date(2024, 1, 3), 0.129)]
            return {s: series for s in symbols if s == "HKDUSD=X"}

        n = fx.backfill_fx_rates(db_session, ["HKD", "USD"], date(2024, 1, 1), date(2024, 1, 31), source=hist)
        assert n == 2  # USD skipped
        assert fx.rate_as_of(db_session, "HKD", date(2024, 1, 3)) == pytest.approx(0.129)
        # Idempotent — re-backfill inserts nothing new.
        assert fx.backfill_fx_rates(db_session, ["HKD"], date(2024, 1, 1), date(2024, 1, 31), source=hist) == 0
