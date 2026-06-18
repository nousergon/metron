"""Lot-based NAV reconstruction core (metron-ops#74).

``_lot_positions_asof`` derives the position held on a past date from the lot timeline —
open lots (still held) + closed lots (held only between open and close). This is what lets
us reconstruct an accurate historical NAV for snapshot-sourced accounts with no replayable
trade feed.
"""

from __future__ import annotations

from datetime import date

from api.services.performance import _lot_positions_asof

# (ticker, qty, cost_basis, open_date)
OPEN = [("AAPL", 6.0, 900.0, date(2025, 1, 15)), ("AAPL", 4.0, 600.0, date(2025, 12, 19))]
# (ticker, qty, cost_basis, open_date, close_date)
CLOSED = [("MSFT", 10.0, 3000.0, date(2024, 6, 1), date(2025, 3, 1))]


def test_before_any_lot_is_empty():
    pos, _cost = _lot_positions_asof(OPEN, CLOSED, date(2024, 1, 1))
    assert dict(pos) == {}


def test_closed_lot_counts_only_within_its_holding_window():
    # MSFT held (opened 2024-06-01, closes 2025-03-01); AAPL not yet opened.
    pos, _ = _lot_positions_asof(OPEN, CLOSED, date(2024, 7, 1))
    assert pos["MSFT"] == 10.0 and "AAPL" not in pos
    # On the close date it's no longer held (exclusive upper bound).
    pos, _ = _lot_positions_asof(OPEN, CLOSED, date(2025, 3, 1))
    assert "MSFT" not in pos


def test_open_lots_accumulate_by_open_date():
    # 2025-02-01: first AAPL lot open (6), second not yet; MSFT still held.
    pos, cost = _lot_positions_asof(OPEN, CLOSED, date(2025, 2, 1))
    assert pos["AAPL"] == 6.0 and cost["AAPL"] == 900.0 and pos["MSFT"] == 10.0
    # 2026-01-01: both AAPL lots open (10), MSFT long closed.
    pos, cost = _lot_positions_asof(OPEN, CLOSED, date(2026, 1, 1))
    assert pos["AAPL"] == 10.0 and cost["AAPL"] == 1500.0 and "MSFT" not in pos
