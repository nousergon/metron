"""Tests for broker_io/transactions.py — activity mapping + tranche reconstruction.

The lot-row presentation tests (TestLotRows) stayed in robodashboard with
``ui/lots.py``, which is view-layer rendering not part of this engine.
"""

from datetime import date

from portfolio_analytics.broker_io.transactions import (
    activities_to_transactions,
    reconstruct_tranches,
)
from portfolio_analytics.domain.ledger import TxnType


def _activity(type_, ticker="AAPL", units=0, price=0, amount=0, fee=0, when="2024-01-15", currency="USD", acct="U1"):
    return {
        "id": f"{type_}-{ticker}-{when}",
        "type": type_,
        "units": units,
        "price": price,
        "amount": amount,
        "fee": fee,
        "trade_date": f"{when}T00:00:00.000Z",
        "symbol": {"symbol": ticker, "currency": {"code": currency}},
        "currency": {"code": currency},
        "account_number": acct,
    }


class TestActivitiesToTransactions:
    def test_buy_sell_mapping(self):
        txns = activities_to_transactions(
            [
                _activity("BUY", units=10, price=100, fee=1),
                _activity("SELL", units=5, price=120, fee=1, when="2024-06-01"),
            ]
        )
        assert [t.type for t in txns] == [TxnType.BUY, TxnType.SELL]
        assert txns[0].quantity == 10 and txns[0].price == 100 and txns[0].fees == 1
        assert txns[0].when == date(2024, 1, 15)

    def test_cash_types_mapped(self):
        txns = activities_to_transactions(
            [
                _activity("CONTRIBUTION", ticker="", amount=5000),
                _activity("WITHDRAWAL", ticker="", amount=1000),
                _activity("DIVIDEND", amount=42),
                _activity("FEE", ticker="", amount=3),
            ]
        )
        assert [t.type for t in txns] == [TxnType.DEPOSIT, TxnType.WITHDRAWAL, TxnType.DIVIDEND, TxnType.FEE]

    def test_unknown_type_skipped(self):
        assert activities_to_transactions([_activity("OPTIONEXPIRATION", units=1)]) == []

    def test_undated_activity_skipped(self):
        a = _activity("BUY", units=10, price=100)
        a["trade_date"] = None
        a["settlement_date"] = None
        assert activities_to_transactions([a]) == []

    def test_currency_and_ticker_extraction(self):
        t = activities_to_transactions([_activity("BUY", ticker="D05.SI", units=1, price=30, currency="SGD")])[0]
        assert t.ticker == "D05.SI"
        assert t.currency == "SGD"

    def test_nested_symbol_shape(self):
        a = _activity("BUY", units=1, price=10)
        a["symbol"] = {"symbol": {"symbol": "MSFT"}, "currency": {"code": "USD"}}
        assert activities_to_transactions([a])[0].ticker == "MSFT"

    def test_missing_symbol_and_currency_fall_back(self):
        # A cash activity with no symbol → empty ticker, USD currency, no crash.
        a = _activity("DIVIDEND", amount=10)
        a["symbol"] = None
        a["currency"] = None
        t = activities_to_transactions([a])[0]
        assert t.ticker == ""
        assert t.currency == "USD"

    def test_non_numeric_units_coerce_to_zero(self):
        a = _activity("BUY", price=100, when="2024-01-15")
        a["units"] = "not-a-number"
        assert activities_to_transactions([a])[0].quantity == 0.0


class TestReconstructTranches:
    def test_two_buys_make_two_lots(self):
        acts = [
            _activity("BUY", units=10, price=100, when="2024-01-15"),
            _activity("BUY", units=5, price=120, when="2024-03-20"),
        ]
        holdings = [{"ticker": "AAPL", "shares": 15, "avg_cost": 106.67, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        assert ts.complete
        assert len(ts.lots) == 2
        assert ts.reconstructed_shares == 15
        assert abs(ts.residual_shares) < 1e-6
        assert not ts.has_synthetic_residual

    def test_fifo_sell_closes_oldest_lot(self):
        acts = [
            _activity("BUY", units=10, price=100, when="2024-01-15"),
            _activity("BUY", units=10, price=120, when="2024-03-20"),
            _activity("SELL", units=12, price=150, when="2024-09-01"),
        ]
        holdings = [{"ticker": "AAPL", "shares": 8, "avg_cost": 120, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        assert ts.complete
        # 12 sold FIFO: all 10 of lot1 + 2 of lot2 → 8 remain, all from lot2 @120.
        assert ts.reconstructed_shares == 8
        assert ts.lots[0].cost_per_share == 120
        assert ts.realized_lt == 0  # < 1 year → short-term
        assert ts.realized_st > 0

    def test_history_incomplete_sell_exceeds_buys(self):
        # Only a SELL is visible (the opening BUY predates available history).
        acts = [_activity("SELL", units=5, price=150, when="2024-09-01")]
        holdings = [{"ticker": "AAPL", "shares": 3, "avg_cost": 100, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        assert ts.complete is False
        assert "history" in ts.note.lower()
        assert "could not be replayed" in ts.note  # errored signal preserved
        # Whole position is treated as pre-history residual.
        assert ts.residual_shares == 3
        assert ts.has_synthetic_residual

    def test_partial_history_seeds_synthetic_residual(self):
        # Visible BUY covers 10 shares but broker holds 25 → 15 predate history.
        acts = [_activity("BUY", units=10, price=100, when="2024-01-15")]
        holdings = [{"ticker": "AAPL", "shares": 25, "avg_cost": 90, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        assert ts.complete is False
        assert ts.has_synthetic_residual
        assert ts.residual_shares == 15
        assert ts.residual_cost_per_share == 90

    def test_no_history_for_held_ticker(self):
        ts = reconstruct_tranches([], [{"ticker": "AAPL", "shares": 5, "avg_cost": 100, "currency": "USD"}])["AAPL"]
        assert ts.complete is False
        assert ts.residual_shares == 5
        assert "No activity history" in ts.note

    def test_fifo_does_not_cross_accounts(self):
        # Same ticker in two accounts; a sell in U2 must not close U1's lot.
        acts = [
            _activity("BUY", units=10, price=100, when="2024-01-15", acct="U1"),
            _activity("BUY", units=10, price=200, when="2024-02-15", acct="U2"),
            _activity("SELL", units=10, price=250, when="2024-09-01", acct="U2"),
        ]
        holdings = [{"ticker": "AAPL", "shares": 10, "avg_cost": 100, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        # U2's sell closes U2's @200 lot; U1's @100 lot survives intact.
        assert ts.reconstructed_shares == 10
        assert ts.lots[0].cost_per_share == 100

    def test_reconstructed_more_than_held_flags_missing_disposal(self):
        # History shows 10 bought but broker holds only 4 → a disposal is missing
        # from the feed. Flag it; never fabricate a negative synthetic lot.
        acts = [_activity("BUY", units=10, price=100, when="2024-01-15")]
        holdings = [{"ticker": "AAPL", "shares": 4, "avg_cost": 100, "currency": "USD"}]
        ts = reconstruct_tranches(acts, holdings)["AAPL"]
        assert ts.complete is False
        assert ts.has_synthetic_residual is False  # residual negative, not synthetic
        assert "missing a disposal" in ts.note
