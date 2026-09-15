"""What-if purchase (metron-ops-I311) — before/after golden fixture, unheld-ticker price
resolution (spine vs. user-entered), and the tax-lot preview line. No score, no ranking:
every candidate here is exactly the ticker the caller typed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from api.db import models
from api.services import whatif_purchase
from portfolio_analytics.prices import ClosePoint

_AS_OF = date(2026, 9, 15)


def _seed(session):
    """AAPL 10@100 (Tech, MV 1000, cost 1000), XOM 10@100 (Energy, MV 1000, cost 1000).
    Equal-weight two-position book, $2,000 total — easy numbers to hand-check."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    session.add(acct)
    session.flush()
    for sym, sector in (("AAPL", "Information Technology"), ("XOM", "Energy")):
        sec = models.Security(symbol=sym, currency="USD", sector=sector, asset_class="EQUITY")
        session.add(sec)
        session.flush()
        session.add(models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2026, 1, 2), source_key=f"buy-{sym}",
        ))
        session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 9, 13), close=100.0, currency="USD"))
    session.commit()
    return tenant.id, pf.id


@dataclass
class _StubFundamentals:
    yf_symbol: str
    dividend_yield: float | None


@dataclass
class _StubSnapshot:
    by_symbol: dict


def _fund_reader(divs: dict[str, float]):
    def _read():
        return _StubSnapshot(by_symbol={s: _StubFundamentals(yf_symbol=s, dividend_yield=y) for s, y in divs.items()})
    return _read


def _spine_reader(prices: dict[str, float]):
    def _read(session, symbols, *, currency_by_symbol=None):
        return {s: ClosePoint(bar_date=_AS_OF, close=prices[s]) for s in symbols if s in prices}
    return _read


class TestConcentrationBeforeAfter:
    def test_buying_an_unheld_ticker_dilutes_existing_concentration(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "MSFT", amount_usd=1000.0, feed_entitled=True,
            as_of=_AS_OF, price_reader=_spine_reader({"MSFT": 250.0}),
            fundamentals_reader=_fund_reader({}),
        )
        # Before: AAPL/XOM equal-weight 50/50 over $2,000 → HHI = 0.5.
        assert plan.before.concentration.n_positions == 2
        assert plan.before.concentration.hhi == pytest.approx(0.5)
        # After: three names over $3,000 (1000/1000/1000) → still equal-weight, HHI = 1/3.
        assert plan.after.concentration.n_positions == 3
        assert plan.after.concentration.hhi == pytest.approx(1.0 / 3.0)
        assert plan.after.concentration.effective_n == pytest.approx(3.0)

    def test_buying_more_of_an_already_held_ticker_concentrates(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "AAPL", amount_usd=1000.0, feed_entitled=True,
            as_of=_AS_OF, fundamentals_reader=_fund_reader({}),
        )
        assert plan.price_source == "held"  # already-held price, no spine call needed
        # After: AAPL 2000, XOM 1000 over 3000 → weights 2/3, 1/3 → HHI = 4/9 + 1/9 = 5/9.
        assert plan.after.concentration.hhi == pytest.approx(5.0 / 9.0)
        assert plan.after.concentration.max_position_ticker == "AAPL"


class TestSectorAndAssetClassMix:
    def test_sector_mix_shifts_toward_the_new_sector(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "MSFT", amount_usd=1000.0, feed_entitled=True,
            as_of=_AS_OF, price_reader=_spine_reader({"MSFT": 250.0}),
            fundamentals_reader=_fund_reader({}),
        )
        # MSFT has no sector seeded in this fixture (Security row not created for it), so
        # it lands in Unclassified — proving the mix reflects the ACTUAL hypothetical
        # holding, not a guessed classification. Zero-weight rows are omitted entirely
        # (mirrors the sector/asset-class mix contract elsewhere), so "before" carries no
        # Unclassified row at all.
        assert not any(r.key == "Unclassified" for r in plan.before.sector_mix)
        after_unclassified = next(r.weight for r in plan.after.sector_mix if r.key == "Unclassified")
        assert after_unclassified == pytest.approx(1000.0 / 3000.0)


class TestUnheldTickerPricing:
    def test_spine_price_used_when_feed_entitled(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "MSFT", amount_usd=500.0, feed_entitled=True,
            as_of=_AS_OF, price_reader=_spine_reader({"MSFT": 250.0}),
            fundamentals_reader=_fund_reader({}),
        )
        assert plan.price_source == "spine"
        assert plan.price == 250.0

    def test_user_entered_price_required_off_feed(self, db_session):
        tid, pid = _seed(db_session)
        with pytest.raises(whatif_purchase.WhatIfPurchaseError):
            whatif_purchase.build_whatif(
                db_session, tid, pid, "MSFT", amount_usd=500.0, feed_entitled=False,
                as_of=_AS_OF, fundamentals_reader=_fund_reader({}),
            )

    def test_user_entered_price_accepted_off_feed(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "MSFT", amount_usd=500.0, user_price=200.0, feed_entitled=False,
            as_of=_AS_OF, fundamentals_reader=_fund_reader({}),
        )
        assert plan.price_source == "user_entered"
        assert plan.price == 200.0
        assert plan.beta_available is False


class TestTaxLotPreview:
    def test_tax_lot_line_matches_the_purchase(self, db_session):
        tid, pid = _seed(db_session)
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "AAPL", amount_usd=500.0, feed_entitled=True,
            as_of=_AS_OF, fundamentals_reader=_fund_reader({}),
        )
        assert plan.tax_lot.symbol == "AAPL"
        assert plan.tax_lot.trade_date == _AS_OF
        assert plan.tax_lot.cost_basis == pytest.approx(plan.usd)
        assert plan.tax_lot.shares == pytest.approx(plan.shares)


class TestDividendYieldOnCost:
    def test_yield_on_cost_is_income_over_cost_not_a_weighted_average_of_ratios(self, db_session):
        tid, pid = _seed(db_session)
        # AAPL yields 2% of its $100 price = $2/share on 10 shares = $20 income on $1000 cost.
        plan = whatif_purchase.build_whatif(
            db_session, tid, pid, "XOM", amount_usd=1000.0, feed_entitled=True,
            as_of=_AS_OF, fundamentals_reader=_fund_reader({"AAPL": 0.02}),
        )
        # Before: only AAPL covered → $20 income / $1000 cost = 2%.
        assert plan.before.dividend_yield_on_cost == pytest.approx(0.02)
