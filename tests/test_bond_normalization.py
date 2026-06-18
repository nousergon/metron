"""Bond par-convention quantity normalization (metron-ops#74).

SnapTrade records a bond/treasury BUY/SELL with quantity=FACE and price=per-$100-par,
so quantity*price ≈ 100*amount. Normalizing quantity → face/100 keeps qty*price ≈ amount
(matching the broker's position section), so the ledger cost basis, realized gains, and
NAV reconstruction aren't 100x inflated. Equity trades pass through unchanged.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from api.services.analytics import _normalize_bond_quantity, _to_engine_txn
from portfolio_analytics.domain.ledger import build_ledger


def test_bond_par_trades_normalized():
    # Live values from the box: 912810UJ5 BUY 10000 @ 97.0147 (amount 9826.12) → 100 units.
    assert _normalize_bond_quantity("BUY", 10000, 97.0147, 9826.12) == 100.0
    assert _normalize_bond_quantity("BUY", 10000, 100.0, 10000.0) == 100.0  # par exactly
    assert _normalize_bond_quantity("SELL", 5000, 98.0391, 4901.95) == 50.0  # 91282CAJ0


def test_equity_trades_unchanged():
    assert _normalize_bond_quantity("BUY", 10, 150.0, 1500.0) == 10.0
    assert _normalize_bond_quantity("SELL", 100, 50.0, 5000.0) == 100.0


def test_non_trade_or_missing_fields_unchanged():
    assert _normalize_bond_quantity("DIVIDEND", 0, 0, 40.0) == 0  # not a BUY/SELL
    assert _normalize_bond_quantity("BUY", 10000, 0, 0) == 10000  # no price/amount → no-op


def test_to_engine_txn_applies_and_skips():
    bond = _to_engine_txn(
        SimpleNamespace(trade_date=date(2025, 5, 20), txn_type="BUY", quantity=10000, price=97.0147, amount=9826.12, fees=0.0, currency="USD"),
        "912810UJ5",
    )
    assert bond.quantity == 100.0 and bond.price == 97.0147
    assert abs(bond.quantity * bond.price - bond.amount) < 0.15 * bond.amount  # now ≈ amount (was 100x off)

    equity = _to_engine_txn(
        SimpleNamespace(trade_date=date(2025, 1, 15), txn_type="BUY", quantity=10, price=150.0, amount=1500.0, fees=1.0, currency="USD"),
        "AAPL",
    )
    assert equity.quantity == 10.0


def test_bond_realized_gain_not_inflated():
    """End-to-end: a bond bought @97 and sold @100 realizes ~$300 over 100 units, NOT
    ~$30,000 (the 100x bug)."""
    buy = _to_engine_txn(
        SimpleNamespace(trade_date=date(2025, 5, 20), txn_type="BUY", quantity=10000, price=97.0, amount=9700.0, fees=0.0, currency="USD"),
        "BOND",
    )
    sell = _to_engine_txn(
        SimpleNamespace(trade_date=date(2026, 5, 20), txn_type="SELL", quantity=10000, price=100.0, amount=10000.0, fees=0.0, currency="USD"),
        "BOND",
    )
    ledger = build_ledger([buy, sell])
    gain = sum(r.gain for r in ledger.realized)
    assert abs(gain - 300.0) < 5.0  # 100 units × ($100 − $97), not 10000 × $3
