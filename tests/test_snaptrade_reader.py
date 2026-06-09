"""Tests for snaptrade_reader.py — raw SnapTrade response parsing.

Covers the two fields that broke silently in the field against real broker
data: the institution name (read from the wrong key) and an unknown cost
basis (a 401(k) CIT with a null ``average_purchase_price``).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from portfolio_analytics.broker_io.snaptrade_reader import SnapTradeReader


def _reader_with_client(client):
    """Build a reader with a stubbed SnapTrade client (no construction/network)."""
    reader = SnapTradeReader.__new__(SnapTradeReader)
    reader._client = client
    reader._user_id = "u"
    reader._user_secret = "s"
    return reader


def test_get_accounts_reads_institution_name():
    """Institution comes from the top-level ``institution_name`` string."""
    client = MagicMock()
    client.account_information.list_user_accounts.return_value = SimpleNamespace(
        body=[
            {
                "id": "1",
                "name": "IBKR Individual",
                "number": "U123",
                "institution_name": "Interactive Brokers",
                # A bare authorization UUID — NOT a nested brokerage object.
                "brokerage_authorization": "04cab1ac-552d-4150-a2a6-932c627177cb",
            },
            {
                "id": "2",
                "name": "BLOCK 401(K) PLAN",
                "number": "*****9430",
                "institution_name": "Fidelity",
                "brokerage_authorization": "094e3f65-c447-4dda-82a7-a60e8e1dfa93",
            },
        ]
    )
    reader = _reader_with_client(client)
    accounts = reader.get_accounts()
    assert [a["institution"] for a in accounts] == ["Interactive Brokers", "Fidelity"]


def test_get_accounts_blank_institution_when_missing():
    """A missing institution_name degrades to '' rather than raising."""
    client = MagicMock()
    client.account_information.list_user_accounts.return_value = SimpleNamespace(
        body=[{"id": "1", "name": "x", "number": "U1"}]
    )
    reader = _reader_with_client(client)
    assert reader.get_accounts()[0]["institution"] == ""


def test_get_holdings_null_cost_basis_falls_back_to_price():
    """A null average_purchase_price (e.g. 401(k) CIT) → cost basis = market value.

    avg_cost defaults to the current price so shares × avg_cost equals market
    value (0 unrealized P&L), not $0 (which would be phantom gain).
    """
    client = MagicMock()
    client.account_information.get_user_holdings.return_value = SimpleNamespace(
        body={
            "positions": [
                {
                    "symbol": {"symbol": {"symbol": "PCKM", "currency": {"code": "USD"}}},
                    "units": 488.811,
                    "price": 20.21,
                    "average_purchase_price": None,
                }
            ]
        }
    )
    reader = _reader_with_client(client)
    pos = reader.get_holdings("acct-1")[0]
    assert pos["avg_cost"] == 20.21
    assert pos["avg_cost"] * pos["shares"] == pos["market_value"]


def test_get_holdings_keeps_real_cost_basis():
    """A real average_purchase_price is preserved untouched."""
    client = MagicMock()
    client.account_information.get_user_holdings.return_value = SimpleNamespace(
        body={
            "positions": [
                {
                    "symbol": {"symbol": {"symbol": "AAPL", "currency": {"code": "USD"}}},
                    "units": 10,
                    "price": 300.0,
                    "average_purchase_price": 110.0,
                }
            ]
        }
    )
    reader = _reader_with_client(client)
    pos = reader.get_holdings("acct-1")[0]
    assert pos["avg_cost"] == 110.0
    assert pos["market_value"] == 3000.0
