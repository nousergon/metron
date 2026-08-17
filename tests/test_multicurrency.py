"""Multi-currency valuation + taxable-only tax view.

The bug this guards: a foreign holding (1299 / HKD) was summed into a USD total at its
native face value. Now native prices convert to the base currency via a cached FX rate,
and a missing rate excludes the holding from the base total rather than fabricating one.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from api.db import models
from api.services import analytics, fx, tax
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint
from portfolio_analytics.prices.symbology import to_yf_symbol


def _price_source(px: dict[str, float]):
    def src(symbols, *, source=None):
        return {s: ClosePoint(date(2026, 6, 9), px[s]) for s in symbols if s in px}

    return src


@pytest.fixture()
def world(db_session):
    """A portfolio (USD base) with a US holding (AAPL) and a HK holding (1299/HKD) in a
    taxable account, plus a 1299 lot in a Roth IRA account."""
    s = db_session
    t = models.Tenant(name="t")
    s.add(t)
    s.flush()
    p = models.Portfolio(tenant_id=t.id, name="p", base_currency="USD")
    s.add(p)
    s.flush()
    taxable = models.Account(tenant_id=t.id, portfolio_id=p.id, broker="ibkr_flex", external_id="U1", currency="USD")
    ira = models.Account(
        tenant_id=t.id, portfolio_id=p.id, broker="ibkr_flex", external_id="U2", currency="USD", account_type="Roth IRA"
    )
    s.add_all([taxable, ira])
    aapl = models.Security(symbol="AAPL", currency="USD", yf_symbol=to_yf_symbol("AAPL", "USD", ""))
    hk = models.Security(symbol="1299", currency="HKD", exchange="SEHK", yf_symbol=to_yf_symbol("1299", "HKD", "SEHK"))
    s.add_all([aapl, hk])
    s.flush()
    # Taxable account: 10 AAPL @ $100, 100 of 1299 @ 60 HKD (broker MV 70 HKD/sh).
    s.add(models.Position(tenant_id=t.id, account_id=taxable.id, security_id=aapl.id, quantity=10, avg_cost=100, currency="USD", as_of=date(2026, 6, 1)))
    s.add(models.Position(tenant_id=t.id, account_id=taxable.id, security_id=hk.id, quantity=100, avg_cost=60, currency="HKD", market_price=70.0, market_value_local=7000.0, as_of=date(2026, 6, 1)))
    s.commit()
    return t.id, p.id


class TestMultiCurrencyValuation:
    def test_duplicate_global_security_row_does_not_hijack_currency(self, db_session, world):
        """metron-ops#274: `securities` is a GLOBAL, cross-tenant table keyed
        (symbol, currency) — a different tenant can legitimately register a
        `("1299", "USD")` row for an unrelated instrument sharing the same broker
        symbol. `_holdings` must resolve THIS tenant's currency from the Security row
        its own Position actually links to, never from an arbitrary "first row for
        this symbol text" pick across the whole shared table — a stray same-symbol,
        different-currency row previously outvoted the real one and rendered a live
        HKD position as an unconverted, ~7.8x-inflated USD figure."""
        tid, pid = world
        # Another tenant's unrelated "1299" security, USD, with a lower sort key than
        # the real HKD row so the old "first by Security.id" pick would choose it.
        decoy = models.Security(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"), symbol="1299", currency="USD"
        )
        db_session.add(decoy)
        db_session.commit()

        # Prices supplied directly (bypassing refresh_latest_prices, a SEPARATE instance
        # of the same symbol-text-lookup pattern in prices._securities_by_symbol — see
        # metron-ops#274 follow-up) to isolate this test to the currency resolution fix
        # under test.
        fx.refresh_fx_rates(db_session, ["HKD"], source=_price_source({"HKDUSD=X": 0.128}))

        held = {
            h.ticker: h
            for h in analytics.valued_holdings(
                db_session, tid, pid,
                prices={"AAPL": ClosePoint(date(2026, 6, 9), 190.0), "1299": ClosePoint(date(2026, 6, 9), 75.0)},
            )
        }
        hk = held["1299"]
        assert hk.currency == "HKD"
        assert hk.market_value == pytest.approx(960.0)  # 7500 HKD * 0.128, NOT 7500 raw

    def test_foreign_holding_converts_to_base(self, db_session, world):
        tid, pid = world
        price_service.refresh_latest_prices(db_session, ["AAPL", "1299"], source=_price_source({"AAPL": 190.0, "1299.HK": 75.0}))
        fx.refresh_fx_rates(db_session, ["HKD"], source=_price_source({"HKDUSD=X": 0.128}))

        held = {h.ticker: h for h in analytics.valued_holdings(db_session, tid, pid)}
        hk = held["1299"]
        assert hk.currency == "HKD"
        assert hk.last_price == pytest.approx(75.0)  # native
        assert hk.market_value_local == pytest.approx(7500.0)  # native
        assert hk.market_value == pytest.approx(960.0)  # 7500 * 0.128 → USD
        assert hk.cost_basis_base == pytest.approx(768.0)  # 6000 HKD * 0.128
        assert hk.unrealized_gain == pytest.approx(192.0)  # base
        # The US holding is unaffected (fx 1.0).
        assert held["AAPL"].market_value == pytest.approx(1900.0)

    def test_summary_total_is_pure_base_currency(self, db_session, world):
        tid, pid = world
        price_service.refresh_latest_prices(db_session, ["AAPL", "1299"], source=_price_source({"AAPL": 190.0, "1299.HK": 75.0}))
        fx.refresh_fx_rates(db_session, ["HKD"], source=_price_source({"HKDUSD=X": 0.128}))
        summ = analytics.summary(db_session, tid, pid)
        # 190*10 + 960 = 2860 (NOT 1900 + 7500 = 9400, the mixed-currency bug).
        assert summ.market_value == pytest.approx(2860.0)
        assert summ.total_cost_basis == pytest.approx(1768.0)  # 1000 + 768
        assert summ.n_unconverted == 0

    def test_missing_fx_excludes_from_base_total_no_fabrication(self, db_session, world):
        tid, pid = world
        # Price the holdings but never cache the HKD rate.
        price_service.refresh_latest_prices(db_session, ["AAPL", "1299"], source=_price_source({"AAPL": 190.0, "1299.HK": 75.0}))
        held = {h.ticker: h for h in analytics.valued_holdings(db_session, tid, pid)}
        hk = held["1299"]
        assert hk.market_value_local == pytest.approx(7500.0)  # native still shown
        assert hk.market_value is None  # but NOT folded into a USD total
        assert hk.cost_basis_base is None
        summ = analytics.summary(db_session, tid, pid)
        assert summ.market_value == pytest.approx(1900.0)  # AAPL only
        assert summ.total_cost_basis == pytest.approx(1000.0)  # AAPL only
        assert summ.n_unconverted == 1  # the HKD holding flagged

    def test_broker_native_price_fallback(self, db_session, world):
        tid, pid = world
        # Only AAPL prices via yfinance; 1299.HK absent → use the broker markPrice (70 HKD).
        price_service.refresh_latest_prices(db_session, ["AAPL", "1299"], source=_price_source({"AAPL": 190.0}))
        fx.refresh_fx_rates(db_session, ["HKD"], source=_price_source({"HKDUSD=X": 0.128}))
        held = {h.ticker: h for h in analytics.valued_holdings(db_session, tid, pid)}
        hk = held["1299"]
        assert hk.last_price == pytest.approx(70.0)  # broker native
        assert hk.market_value == pytest.approx(100 * 70 * 0.128)  # 896 USD


class TestTaxableFilter:
    def test_tax_excludes_tax_advantaged_accounts(self, db_session, world):
        tid, pid = world
        # Add a 1299 position to the Roth IRA so the two accounts differ.
        s = db_session
        ira = s.query(models.Account).filter(models.Account.external_id == "U2").one()
        hk = s.query(models.Security).filter(models.Security.symbol == "1299").one()
        s.add(models.Position(tenant_id=tid, account_id=ira.id, security_id=hk.id, quantity=50, avg_cost=60, currency="HKD", as_of=date(2026, 6, 1)))
        s.commit()
        # Note: tax_lots reads the ledger (transactions), and these are position-only
        # accounts, so it has no lots — but the account-exclusion count is what we assert.
        summary = tax.tax_lots(db_session, tid, pid, today=date(2026, 6, 9), taxable_only=True)
        assert summary.n_accounts_excluded == 1  # the Roth IRA
        assert summary.base_currency == "USD"
