"""Tests for loaders/realized.py — realized income from the activity feed."""

from datetime import date

from portfolio_analytics.broker_io.realized_income import build_realized_income
from portfolio_analytics.domain.ledger import RealizedGain


def _activity(type_, ticker="AAPL", units=0, price=0, amount=0, fee=0, when="2026-03-15", acct="U1"):
    return {
        "type": type_,
        "units": units,
        "price": price,
        "amount": amount,
        "fee": fee,
        "trade_date": f"{when}T00:00:00.000Z",
        "symbol": {"symbol": ticker, "currency": {"code": "USD"}},
        "account_number": acct,
    }


def test_none_when_no_activities():
    assert build_realized_income([]) is None


def test_none_when_no_activities_and_no_extra():
    assert build_realized_income([], extra_realized=[]) is None


def test_extra_realized_renders_with_empty_feed():
    """IBKR Flex lots populate the realized view even when the SnapTrade feed is empty."""
    lot = RealizedGain(
        ticker="NVDA",
        open_date=date(2024, 1, 2),
        close_date=date(2026, 3, 1),
        quantity=4,
        proceeds=4000,
        cost_basis=2500,
    )
    out = build_realized_income([], extra_realized=[lot])
    assert out is not None
    assert len(out["years"]) == 1
    y = out["years"][0]
    assert y.year == 2026
    assert y.realized_lt == 1500  # held > 1 year, 4000 − 2500
    assert out["incomplete"] == []  # IBKR lots never flagged incomplete
    assert out["detail"][0]["ticker"] == "NVDA"


def test_extra_realized_merges_with_feed_lots():
    """A feed-reconstructed gain and an IBKR Flex gain both land in the same year."""
    acts = [
        _activity("BUY", units=10, price=100, when="2024-01-10"),
        _activity("SELL", units=10, price=150, when="2026-02-01"),
    ]
    ibkr = RealizedGain(
        ticker="AMD",
        open_date=date(2025, 12, 1),
        close_date=date(2026, 6, 1),
        quantity=5,
        proceeds=1000,
        cost_basis=1200,
    )
    out = build_realized_income(acts, extra_realized=[ibkr])
    y = next(y for y in out["years"] if y.year == 2026)
    assert y.realized_lt == 500  # feed AAPL long-term gain
    assert y.realized_st == -200  # IBKR AMD short-term loss


def test_realized_capital_gain_in_close_year():
    acts = [
        _activity("BUY", units=10, price=100, when="2024-01-10"),
        _activity("SELL", units=10, price=150, when="2026-02-01"),
    ]
    out = build_realized_income(acts)
    assert len(out["years"]) == 1
    y = out["years"][0]
    assert y.year == 2026
    assert y.realized_lt == 500  # held > 1 year → long-term, (150-100)*10
    assert y.realized_st == 0
    assert out["incomplete"] == []
    assert len(out["detail"]) == 1
    assert out["detail"][0]["term"] == "Long-term"


def test_short_term_gain():
    acts = [
        _activity("BUY", units=5, price=100, when="2026-01-10"),
        _activity("SELL", units=5, price=120, when="2026-06-10"),
    ]
    y = build_realized_income(acts)["years"][0]
    assert y.realized_st == 100
    assert y.realized_lt == 0


def test_dividends_and_interest_summed_by_year():
    acts = [
        _activity("DIVIDEND", amount=300, when="2026-04-01"),
        _activity("DIVIDEND", amount=200, when="2026-07-01"),
        _activity("INTEREST", amount=42, when="2026-05-01"),
    ]
    out = build_realized_income(acts)
    y = out["years"][0]
    assert y.dividends == 500
    assert y.interest == 42
    assert y.net_capital_gains == 0
    assert y.taxable_income == 542


def test_account_filter_restricts_scope():
    acts = [
        _activity("BUY", units=10, price=100, when="2024-01-10", acct="U1"),
        _activity("SELL", units=10, price=150, when="2026-02-01", acct="U1"),
        _activity("DIVIDEND", amount=999, when="2026-04-01", acct="U2"),  # IRA — excluded
    ]
    out = build_realized_income(acts, account_numbers=["U1"])
    y = out["years"][0]
    assert y.realized_lt == 500
    assert y.dividends == 0  # U2 dividend filtered out


def test_incomplete_history_flagged_and_gain_dropped():
    # A SELL with no visible BUY → cost basis unrecoverable → ticker flagged.
    acts = [_activity("SELL", ticker="OLD", units=5, price=150, when="2026-02-01")]
    out = build_realized_income(acts)
    assert out["incomplete"] == ["OLD"]
    assert out["years"] == []  # no replayable realized gains


def test_undated_income_activity_is_skipped():
    # A dividend with no parseable date can't be bucketed → skipped, not crashed.
    div = _activity("DIVIDEND", amount=100, when="2026-04-01")
    undated = _activity("DIVIDEND", amount=999)
    undated["trade_date"] = None
    undated["settlement_date"] = None
    y = build_realized_income([div, undated])["years"][0]
    assert y.dividends == 100


def test_per_account_fifo_does_not_cross_accounts():
    # Same ticker, two accounts; a sell in U2 closes U2's lot only.
    acts = [
        _activity("BUY", units=10, price=100, when="2024-01-10", acct="U1"),
        _activity("BUY", units=10, price=200, when="2024-02-10", acct="U2"),
        _activity("SELL", units=10, price=250, when="2026-03-10", acct="U2"),
    ]
    y = build_realized_income(acts)["years"][0]
    assert y.realized_lt == 500  # (250-200)*10 from U2's lot, not U1's @100
