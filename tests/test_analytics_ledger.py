"""Tests for analytics/ledger.py — tax lots, realized gains, splits, cash flows."""

from __future__ import annotations

from datetime import date

import pytest
from nousergon_lib.quant.returns import CashFlow, xirr

from portfolio_analytics.domain.ledger import (
    Transaction,
    TxnType,
    build_ledger,
    external_cash_flows,
)


def _buy(when, ticker, qty, price, fees=0.0):
    return Transaction(when, TxnType.BUY, ticker=ticker, quantity=qty, price=price, fees=fees)


def _sell(when, ticker, qty, price, fees=0.0):
    return Transaction(when, TxnType.SELL, ticker=ticker, quantity=qty, price=price, fees=fees)


class TestPositions:
    def test_single_buy_position(self):
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0)])
        shares, avg = led.position("AAPL")
        assert shares == 10
        assert avg == pytest.approx(100.0)

    def test_fees_fold_into_cost_basis(self):
        # 10 sh @ $100 + $10 fee → avg cost 101/sh.
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0, fees=10.0)])
        _, avg = led.position("AAPL")
        assert avg == pytest.approx(101.0)

    def test_average_cost_across_lots(self):
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0), _buy(date(2025, 2, 1), "AAPL", 10, 120.0)])
        shares, avg = led.position("AAPL")
        assert shares == 20
        assert avg == pytest.approx(110.0)

    def test_money_market_buy_with_no_price_uses_amount(self):
        # FDRXX-style sweep: broker reports quantity + cash amount but price=0 (metron-ops#61).
        # Cost basis must come from `amount`, not collapse to $0.
        txn = Transaction(date(2025, 1, 1), TxnType.BUY, ticker="FDRXX", quantity=10000, price=0.0, amount=10000.0)
        led = build_ledger([txn])
        shares, avg = led.position("FDRXX")
        assert shares == 10000
        assert avg == pytest.approx(1.0)
        assert led.cash == pytest.approx(-10000.0)  # cash debited by the true amount

    def test_priced_buy_ignores_amount(self):
        # When price IS reported it stays authoritative — amount must not override it.
        txn = Transaction(date(2025, 1, 1), TxnType.BUY, ticker="AAPL", quantity=10, price=100.0, amount=999.0)
        led = build_ledger([txn])
        _, avg = led.position("AAPL")
        assert avg == pytest.approx(100.0)

    def test_no_position(self):
        led = build_ledger([])
        assert led.position("AAPL") == (0.0, 0.0)


class TestFifoRealized:
    def test_fifo_closes_oldest_first(self):
        # Buy 10@100, buy 10@120, sell 10@150 → closes the $100 lot → gain 500.
        led = build_ledger(
            [
                _buy(date(2025, 1, 1), "AAPL", 10, 100.0),
                _buy(date(2025, 2, 1), "AAPL", 10, 120.0),
                _sell(date(2025, 3, 1), "AAPL", 10, 150.0),
            ]
        )
        assert len(led.realized) == 1
        assert led.realized[0].gain == pytest.approx(500.0)  # (150-100)*10
        # Remaining open = the $120 lot.
        shares, avg = led.position("AAPL")
        assert shares == 10
        assert avg == pytest.approx(120.0)

    def test_partial_lot_close(self):
        # Sell 5 of a 10-share lot → 5 realized, 5 remain.
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0), _sell(date(2025, 6, 1), "AAPL", 5, 130.0)])
        assert led.realized[0].quantity == 5
        assert led.realized[0].gain == pytest.approx(150.0)  # (130-100)*5
        assert led.position("AAPL")[0] == 5

    def test_sell_spanning_two_lots(self):
        # Sell 15 across a 10-lot then a 10-lot → 2 realized entries.
        led = build_ledger(
            [
                _buy(date(2025, 1, 1), "AAPL", 10, 100.0),
                _buy(date(2025, 2, 1), "AAPL", 10, 200.0),
                _sell(date(2025, 3, 1), "AAPL", 15, 250.0),
            ]
        )
        assert len(led.realized) == 2
        assert led.realized[0].gain == pytest.approx(1500.0)  # (250-100)*10
        assert led.realized[1].gain == pytest.approx(250.0)  # (250-200)*5
        assert led.position("AAPL")[0] == 5

    def test_sell_more_than_held_raises(self):
        with pytest.raises(ValueError, match="exceeds"):
            build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0), _sell(date(2025, 2, 1), "AAPL", 11, 100.0)])

    def test_sell_fees_reduce_proceeds(self):
        # Sell 10@100 with $20 fee → proceeds 980, gain (980 - 900) = 80 over a 90 basis.
        led = build_ledger(
            [_buy(date(2025, 1, 1), "AAPL", 10, 90.0), _sell(date(2025, 2, 1), "AAPL", 10, 100.0, fees=20.0)]
        )
        assert led.realized[0].proceeds == pytest.approx(980.0)
        assert led.realized[0].gain == pytest.approx(80.0)


class TestHoldingPeriod:
    def test_short_term(self):
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0), _sell(date(2025, 6, 1), "AAPL", 10, 110.0)])
        assert led.realized[0].long_term is False

    def test_long_term_over_365_days(self):
        led = build_ledger([_buy(date(2024, 1, 1), "AAPL", 10, 100.0), _sell(date(2025, 6, 1), "AAPL", 10, 110.0)])
        assert led.realized[0].long_term is True

    def test_exactly_365_days_is_short_term(self):
        # "More than one year" → 365 days exactly is still short-term.
        led = build_ledger([_buy(date(2024, 1, 1), "AAPL", 10, 100.0), _sell(date(2024, 12, 31), "AAPL", 10, 110.0)])
        assert led.realized[0].holding_days == 365
        assert led.realized[0].long_term is False

    def test_realized_totals_split(self):
        led = build_ledger(
            [
                _buy(date(2024, 1, 1), "AAPL", 10, 100.0),  # held >1y
                _buy(date(2025, 1, 1), "MSFT", 10, 100.0),  # held <1y
                _sell(date(2025, 6, 1), "AAPL", 10, 150.0),  # long-term +500
                _sell(date(2025, 6, 1), "MSFT", 10, 120.0),  # short-term +200
            ]
        )
        st, lt = led.realized_totals()
        assert st == pytest.approx(200.0)
        assert lt == pytest.approx(500.0)


class TestSplits:
    def test_two_for_one_split_preserves_basis(self):
        # 10@100 (basis 1000), 2:1 split → 20 shares @ 50, basis still 1000.
        led = build_ledger(
            [
                _buy(date(2025, 1, 1), "AAPL", 10, 100.0),
                Transaction(date(2025, 2, 1), TxnType.SPLIT, ticker="AAPL", quantity=2.0),
            ]
        )
        shares, avg = led.position("AAPL")
        assert shares == 20
        assert avg == pytest.approx(50.0)

    def test_split_with_no_lots_is_noop(self):
        led = build_ledger([Transaction(date(2025, 1, 1), TxnType.SPLIT, ticker="AAPL", quantity=2.0)])
        assert led.position("AAPL") == (0.0, 0.0)


class TestUnrealizedAndCash:
    def test_unrealized(self):
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0)])
        # Mark at 130 → unrealized 300.
        assert led.unrealized({"AAPL": 130.0})["AAPL"] == pytest.approx(300.0)

    def test_unrealized_skips_unpriced(self):
        led = build_ledger([_buy(date(2025, 1, 1), "AAPL", 10, 100.0)])
        assert led.unrealized({}) == {}

    def test_cash_tracks_deposits_buys_dividends(self):
        led = build_ledger(
            [
                Transaction(date(2025, 1, 1), TxnType.DEPOSIT, amount=10000.0),
                _buy(date(2025, 1, 2), "AAPL", 10, 100.0, fees=5.0),  # -1005
                Transaction(date(2025, 2, 1), TxnType.DIVIDEND, ticker="AAPL", amount=50.0),  # +50
            ]
        )
        assert led.cash == pytest.approx(10000.0 - 1005.0 + 50.0)


class TestExternalCashFlows:
    def test_only_deposits_and_withdrawals_with_signs(self):
        txns = [
            Transaction(date(2025, 1, 1), TxnType.DEPOSIT, amount=10000.0),
            _buy(date(2025, 1, 2), "AAPL", 10, 100.0),  # internal — excluded
            Transaction(date(2025, 6, 1), TxnType.WITHDRAWAL, amount=2000.0),
        ]
        flows = external_cash_flows(txns)
        assert flows == [CashFlow(date(2025, 1, 1), -10000.0), CashFlow(date(2025, 6, 1), 2000.0)]

    def test_feeds_xirr_end_to_end(self):
        # Deposit 100, it grows to 200 in a year (terminal value appended) → 100% MWR.
        txns = [Transaction(date(2025, 1, 1), TxnType.DEPOSIT, amount=100.0)]
        flows = external_cash_flows(txns) + [CashFlow(date(2026, 1, 1), 200.0)]
        assert xirr(flows) == pytest.approx(1.0, abs=1e-4)
