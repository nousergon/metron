"""Dashboard accuracy verification framework, layer 4 (metron-ops#210): golden
portfolios + property-based tests over ``portfolio_analytics.domain.ledger``.

Two independent verification strategies, per the epic:

1. **Golden fixtures** — small, hand-verified portfolios where cost basis, realized
   gain, and total return are computed BY HAND in the comments below (the arithmetic
   is shown so the "hand-verified" claim is auditable, not asserted-against-itself),
   then checked against the engine's own ``build_ledger`` output.

2. **Property tests** (hypothesis) — randomized-but-valid transaction histories
   (never selling more than currently held, the one hard invariant ``build_ledger``
   itself enforces) asserting structural identities that must hold for ANY such
   history, not just hand-picked cases:
     - Σ lot quantities == ``Ledger.position()`` quantity (the position accessor is
       just a sum/weighted-average over the same ``open_lots`` list, but the property
       test guards that relationship against future refactors of either side).
     - realized total + unrealized total == total P&L, where "total P&L" is computed
       by an INDEPENDENT accounting identity (total cash proceeds/market-value out
       minus total cost deployed in) rather than by re-deriving it from the ledger's
       own realized/unrealized fields — so the test can't pass by tautology.

Both exercise ``build_ledger`` (the real production code path), not a re-implementation
of it — this is the "golden portfolios + property tests" layer, distinct from layer 3's
independent shadow-recompute code path (tracked separately, metron-ops#210 child issues).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from portfolio_analytics.domain.ledger import (
    Transaction,
    TxnType,
    build_ledger,
)

# ── Golden fixture 1: from-scratch hand-verified portfolio ──────────────────────
#
# A single ticker, two buys (different cost basis + fees), one sell spanning both
# lots (FIFO), one dividend. Every number below is computed by hand; the test
# asserts the engine reproduces it exactly.
#
# Transactions (chronological):
#   1. 2025-01-02  BUY  10 sh @ $150.00, fee $5.00
#   2. 2025-02-15  BUY  10 sh @ $170.00, fee $5.00
#   3. 2025-06-01  SELL 12 sh @ $200.00, fee $12.00
#   4. 2025-07-01  DIVIDEND $16.00
#
# --- Hand computation ---
#
# Lot 1 (buy 1): cost/share = price + fees/qty = 150 + 5/10        = $150.50
#                lot cost basis = 10 * 150.50                       = $1,505.00
#                cash out = qty*price + fees = 10*150 + 5           = $1,505.00
#
# Lot 2 (buy 2): cost/share = 170 + 5/10                            = $170.50
#                lot cost basis = 10 * 170.50                       = $1,705.00
#                cash out = 10*170 + 5                               = $1,705.00
#
# Sell 12 @ $200, fee $12: net proceeds/share = price - fees/qty
#                                              = 200 - 12/12          = $199.00/sh
#   FIFO closes the oldest lot first: 10 sh from lot 1, then 2 sh from lot 2.
#     Lot-1 portion (10 sh): proceeds = 10*199 = $1,990.00
#                            cost     = 10*150.50 = $1,505.00
#                            gain     = 1,990.00 - 1,505.00 = $485.00
#     Lot-2 portion (2 sh):  proceeds = 2*199 = $398.00
#                            cost     = 2*170.50 = $341.00
#                            gain     = 398.00 - 341.00 = $57.00
#   Total realized gain = 485.00 + 57.00 = $542.00
#   Sell cash in = qty*price - fees = 12*200 - 12 = $2,388.00
#
# Remaining open position: lot 2's un-closed remainder = 10 - 2 = 8 sh @ $170.50/sh
#   Remaining cost basis = 8 * 170.50 = $1,364.00
#
# Dividend: +$16.00 cash, no position/basis effect.
#
# Cash balance = -1,505.00 (buy1) - 1,705.00 (buy2) + 2,388.00 (sell) + 16.00 (div)
#              = -806.00
#
# Mark the remaining 8 sh @ $210.00 (a chosen valuation price, distinct from any
# transaction price, so unrealized isn't accidentally testing a cost-basis passthrough):
#   Unrealized gain = 8*210.00 - 1,364.00 = 1,680.00 - 1,364.00 = $316.00
#
# Total P&L (realized + unrealized) = 542.00 + 316.00 = $858.00
#
# Cross-check via an INDEPENDENT accounting identity (total value received/held minus
# total capital deployed, without going through realized/unrealized at all):
#   Capital deployed  = lot1 basis + lot2 basis = 1,505.00 + 1,705.00 = $3,210.00
#   Value out+held    = sell cash in + dividend + remaining mkt value
#                      = 2,388.00 + 16.00 + 8*210.00 = 2,388.00 + 16.00 + 1,680.00
#                      = $4,084.00
#   Total return (excl. dividend, capital-only) = (4,084.00 - 16.00 - 3,210.00) / 3,210.00
#                                                = 858.00 / 3,210.00
#                                                = 0.267289... (26.7289%)


def _golden_transactions() -> list[Transaction]:
    return [
        Transaction(date(2025, 1, 2), TxnType.BUY, ticker="AAPL", quantity=10, price=150.0, fees=5.0),
        Transaction(date(2025, 2, 15), TxnType.BUY, ticker="AAPL", quantity=10, price=170.0, fees=5.0),
        Transaction(date(2025, 6, 1), TxnType.SELL, ticker="AAPL", quantity=12, price=200.0, fees=12.0),
        Transaction(date(2025, 7, 1), TxnType.DIVIDEND, ticker="AAPL", amount=16.0),
    ]


class TestGoldenPortfolioHandVerified:
    def test_open_lots_after_fifo_sell(self):
        led = build_ledger(_golden_transactions())
        shares, avg_cost = led.position("AAPL")
        assert shares == pytest.approx(8.0)
        assert avg_cost == pytest.approx(170.50)

    def test_realized_gain_matches_hand_computed_fifo_split(self):
        led = build_ledger(_golden_transactions())
        assert len(led.realized) == 2
        lot1_portion, lot2_portion = led.realized
        assert lot1_portion.quantity == pytest.approx(10.0)
        assert lot1_portion.proceeds == pytest.approx(1990.0)
        assert lot1_portion.cost_basis == pytest.approx(1505.0)
        assert lot1_portion.gain == pytest.approx(485.0)

        assert lot2_portion.quantity == pytest.approx(2.0)
        assert lot2_portion.proceeds == pytest.approx(398.0)
        assert lot2_portion.cost_basis == pytest.approx(341.0)
        assert lot2_portion.gain == pytest.approx(57.0)

        total_realized = sum(r.gain for r in led.realized)
        assert total_realized == pytest.approx(542.0)

    def test_cash_balance_matches_hand_computed(self):
        led = build_ledger(_golden_transactions())
        assert led.cash == pytest.approx(-806.0)

    def test_unrealized_and_total_pl_at_mark(self):
        led = build_ledger(_golden_transactions())
        unrealized = led.unrealized({"AAPL": 210.0})
        assert unrealized["AAPL"] == pytest.approx(316.0)

        total_realized = sum(r.gain for r in led.realized)
        total_pl = total_realized + unrealized["AAPL"]
        assert total_pl == pytest.approx(858.0)

    def test_total_return_matches_independent_capital_accounting(self):
        """Cross-checks total P&L against an accounting identity computed WITHOUT
        touching ``realized``/``unrealized`` at all — capital deployed vs. capital
        recovered (sell proceeds) + capital still held (mark-to-market), net of the
        dividend (a yield component, not a capital gain)."""
        led = build_ledger(_golden_transactions())
        capital_deployed = 1505.0 + 1705.0  # both buys' cost basis
        sell_cash_in = 2388.0
        dividend = 16.0
        remaining_mkt_value = 8 * 210.0
        total_return_capital_only = (
            sell_cash_in + remaining_mkt_value - capital_deployed
        ) / capital_deployed
        assert total_return_capital_only == pytest.approx(858.0 / 3210.0)
        assert total_return_capital_only == pytest.approx(0.267289, abs=1e-5)

        # And this independent capital-accounting total must equal realized+unrealized
        # expressed as a fraction of capital deployed — the two ways of asking "how much
        # did the position gain" must agree.
        total_pl = sum(r.gain for r in led.realized) + led.unrealized({"AAPL": 210.0})["AAPL"]
        assert total_pl / capital_deployed == pytest.approx(total_return_capital_only)

        # Cash ledger sanity: capital in (buys) - capital recovered (sell) - dividend
        # equals the negative of the cash balance (money still "out" in the position).
        assert -led.cash == pytest.approx(capital_deployed - sell_cash_in - dividend)


# ── Golden fixture 2: Showcase Portfolio sample sleeve (api/services/demo.py) ────
#
# The frozen, hand-editable sample-sleeve CSV from the Showcase Portfolio fixture
# (``_SAMPLE_SLEEVE_CSV`` in api/services/demo.py) — reproduced here as ledger
# ``Transaction`` objects (bypassing the DB/CSV-import machinery, which needs a live
# session) and checked against an independently, by-hand computed cost basis. This
# is the epic's "the public Showcase Portfolio becomes one of them" case: a second,
# independently-sourced golden case alongside (not instead of) the from-scratch
# fixture above.
#
# Fixture rows (date, type, symbol, quantity, price, amount):
#   2024-01-08  BUY  VOO        15   440   6600   (price*qty = 6600, matches amount)
#   2024-02-02  BUY  912828YK0  50    98   4900   (price*qty = 4900, matches amount)
#   2024-03-15  BUY  VMFXX    2000     1   2000   (price*qty = 2000, matches amount)
#   2024-06-03  DIVIDEND VOO    0     0     38
#
# No fees in this fixture and no SELLs, so cost basis is simply qty*price per lot
# (``_buy`` uses ``price`` when price > 0, which it is for every row here — see
# ``ledger._buy``): no FIFO relief to reason about, just three untouched open lots.
#
# --- Hand computation ---
#   VOO:       15 sh @ $440.00/sh  -> cost basis = 15*440   = $6,600.00
#   912828YK0: 50 sh @ $98.00/sh   -> cost basis = 50*98    = $4,900.00
#   VMFXX:   2000 sh @ $1.00/sh    -> cost basis = 2000*1   = $2,000.00
#   Total cost basis across the sleeve                       = $13,500.00
#   Cash consumed by buys = 6600 + 4900 + 2000                = $13,500.00
#   Dividend received (VOO)                                   = $38.00
#   Net cash balance = -13,500.00 + 38.00                     = -$13,462.00
#
# Frozen EOD closes as-of 2024-06-28 (``_SAMPLE_SLEEVE_PRICES`` in demo.py):
#   VOO $490.00, 912828YK0 $99.00, VMFXX $1.00
#   Unrealized: VOO       = 15*490 - 6600     = 7350 - 6600   = $750.00
#               912828YK0 = 50*99  - 4900     = 4950 - 4900   = $50.00
#               VMFXX     = 2000*1 - 2000     = 2000 - 2000   = $0.00
#   Total unrealized                                           = $800.00
#   No sells -> total realized gain = $0.00 -> total P&L = $800.00


def _showcase_sample_sleeve_transactions() -> list[Transaction]:
    """Transcribed from ``api/services/demo.py::_SAMPLE_SLEEVE_CSV`` — same dates,
    tickers, quantities, prices as the live Showcase Portfolio fixture. If that CSV
    ever changes, this list (and the hand computation above) must be updated to match
    — a deliberate coupling, not a coincidence, so this golden case tracks the actual
    showcase data rather than drifting into a stale parallel fixture."""
    return [
        Transaction(date(2024, 1, 8), TxnType.BUY, ticker="VOO", quantity=15, price=440.0),
        Transaction(date(2024, 2, 2), TxnType.BUY, ticker="912828YK0", quantity=50, price=98.0),
        Transaction(date(2024, 3, 15), TxnType.BUY, ticker="VMFXX", quantity=2000, price=1.0),
        Transaction(date(2024, 6, 3), TxnType.DIVIDEND, ticker="VOO", amount=38.0),
    ]


class TestShowcaseSampleSleeveGolden:
    def test_cost_basis_per_ticker(self):
        led = build_ledger(_showcase_sample_sleeve_transactions())
        assert led.position("VOO") == (pytest.approx(15.0), pytest.approx(440.0))
        assert led.position("912828YK0") == (pytest.approx(50.0), pytest.approx(98.0))
        assert led.position("VMFXX") == (pytest.approx(2000.0), pytest.approx(1.0))

    def test_cash_balance(self):
        led = build_ledger(_showcase_sample_sleeve_transactions())
        assert led.cash == pytest.approx(-13462.0)

    def test_unrealized_at_frozen_eod_closes(self):
        led = build_ledger(_showcase_sample_sleeve_transactions())
        prices = {"VOO": 490.0, "912828YK0": 99.0, "VMFXX": 1.0}
        unrealized = led.unrealized(prices)
        assert unrealized["VOO"] == pytest.approx(750.0)
        assert unrealized["912828YK0"] == pytest.approx(50.0)
        assert unrealized["VMFXX"] == pytest.approx(0.0)
        assert sum(unrealized.values()) == pytest.approx(800.0)
        # No sells in this fixture -> realized is empty -> total P&L is pure unrealized.
        assert led.realized == []


# ── Property-based tests over randomized-but-valid transaction histories ────────
#
# Strategy: build a chronological sequence of BUY/SELL/DIVIDEND transactions on a
# single ticker, tracking running share count so a SELL never exceeds shares held
# (the one hard invariant ``build_ledger`` enforces itself, by raising ValueError).
# Prices/quantities/fees are bounded to sane magnitudes so floating-point comparison
# tolerances stay meaningful (this is a real accounting identity check, not a
# numerical-stability stress test — that's a separate concern).

_TICKER = "PROP"
# Floor for a generated SELL quantity — keeps `held` from decaying into a remainder so
# small that [min_value, held] becomes a degenerate (empty) float range.
_MIN_SELL_QTY = 0.01


@st.composite
def _valid_transaction_history(draw) -> list[Transaction]:
    n_events = draw(st.integers(min_value=1, max_value=25))
    held = 0.0
    when = date(2024, 1, 1)
    txns: list[Transaction] = []
    for _ in range(n_events):
        when = when + timedelta(days=draw(st.integers(min_value=1, max_value=20)))
        price = draw(st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False))
        fees = draw(st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False))

        # Choose BUY when nothing sellable is held yet (a SELL/DIVIDEND on an empty
        # book is either a no-op or meaningless), otherwise let hypothesis pick among
        # all three. _MIN_SELL_QTY floor keeps the SELL branch's [min, held] float
        # range always non-degenerate (a `held` that decayed to a sub-min-float
        # remainder would make `min_value > max_value` and hypothesis would raise).
        kind = "BUY" if held < _MIN_SELL_QTY else draw(st.sampled_from(["BUY", "SELL", "DIVIDEND"]))

        if kind == "BUY":
            qty = draw(st.floats(min_value=1.0, max_value=500.0, allow_nan=False, allow_infinity=False))
            txns.append(Transaction(when, TxnType.BUY, ticker=_TICKER, quantity=qty, price=price, fees=fees))
            held += qty
        elif kind == "SELL":
            # Never exceed what's currently held — the hard invariant build_ledger enforces.
            qty = draw(st.floats(min_value=_MIN_SELL_QTY, max_value=held, allow_nan=False, allow_infinity=False))
            txns.append(Transaction(when, TxnType.SELL, ticker=_TICKER, quantity=qty, price=price, fees=fees))
            held -= qty
        else:  # DIVIDEND — no share-count effect
            amount = draw(st.floats(min_value=0.0, max_value=500.0, allow_nan=False, allow_infinity=False))
            txns.append(Transaction(when, TxnType.DIVIDEND, ticker=_TICKER, amount=amount))
    return txns


@given(txns=_valid_transaction_history())
@settings(max_examples=200)
def test_sum_of_open_lot_quantities_equals_position_quantity(txns: list[Transaction]):
    """Sigma lot quantities == Ledger.position() quantity, for any valid history."""
    led = build_ledger(txns)
    lots = led.open_lots.get(_TICKER, [])
    assert sum(lot.quantity for lot in lots) == pytest.approx(led.position(_TICKER)[0], abs=1e-6)


@given(txns=_valid_transaction_history())
@settings(max_examples=200)
def test_realized_plus_unrealized_equals_independent_total_pl(txns: list[Transaction]):
    """realized + unrealized == total P&L, where "total P&L" is computed by an
    INDEPENDENT identity (total capital in vs. total capital recovered + capital still
    held at a mark), not by re-deriving it from the ledger's own realized/unrealized
    output. Excludes dividends (a distinct income stream, not a capital gain/loss) from
    both sides of the comparison so the identity is exact."""
    txns = [t for t in txns if t.type is not TxnType.DIVIDEND] or txns  # keep at least the buys
    assume(any(t.type is TxnType.BUY for t in txns))
    led = build_ledger(txns)

    # Independent total: sum of every BUY's cash outlay (capital in) vs. every SELL's
    # cash inflow (capital recovered) plus the remaining position's cost basis marked
    # at an arbitrary-but-fixed mark price (capital still held, valued).
    mark_price = 137.0
    capital_in = 0.0
    capital_recovered = 0.0
    for t in txns:
        if t.type is TxnType.BUY and t.quantity > 0:
            capital_in += (t.quantity * t.price + t.fees) if t.price > 0 else t.fees
        elif t.type is TxnType.SELL and t.quantity > 0:
            capital_recovered += t.quantity * t.price - t.fees

    shares_held, _ = led.position(_TICKER)
    capital_still_held = shares_held * mark_price

    independent_total_pl = capital_recovered + capital_still_held - capital_in

    ledger_total_realized = sum(r.gain for r in led.realized)
    ledger_total_unrealized = sum(led.unrealized({_TICKER: mark_price}).values())
    ledger_total_pl = ledger_total_realized + ledger_total_unrealized

    assert ledger_total_pl == pytest.approx(independent_total_pl, abs=1e-4, rel=1e-6)


@given(txns=_valid_transaction_history())
@settings(max_examples=200)
def test_realized_gain_quantities_sum_to_total_shares_sold(txns: list[Transaction]):
    """Every share SOLD shows up as realized-gain quantity exactly once (FIFO lot
    relief partitions a sell across closed lots but never drops or double-counts a
    share) — Sigma RealizedGain.quantity == Sigma SELL.quantity."""
    led = build_ledger(txns)
    total_sold = sum(t.quantity for t in txns if t.type is TxnType.SELL and t.quantity > 0)
    total_realized_qty = sum(r.quantity for r in led.realized)
    assert total_realized_qty == pytest.approx(total_sold, abs=1e-6)


@given(txns=_valid_transaction_history())
@settings(max_examples=200)
def test_selling_more_than_held_always_raises(txns: list[Transaction]):
    """The one hard invariant build_ledger enforces itself: appending a SELL for one
    share more than is ever held after a valid history must raise, never silently
    clamp. Regression guard for the invariant the property strategy above relies on
    to generate exclusively valid histories."""
    led = build_ledger(txns)
    shares_held, _ = led.position(_TICKER)
    last_date = max((t.when for t in txns), default=date(2024, 1, 1))
    overdraft = Transaction(
        last_date + timedelta(days=1), TxnType.SELL, ticker=_TICKER, quantity=shares_held + 1.0, price=100.0
    )
    with pytest.raises(ValueError, match="exceeds"):
        build_ledger([*txns, overdraft])
