"""SnapTradeConnector + CanonicalReader parity tests (PR2a).

The load-bearing guarantee for the flag-gated cutover: routing reads through the
canonical ingestion layer produces output **identical** to reading the SnapTrade
reader directly. We assert that across every consumer surface (accounts, NAV,
aggregated holdings, realized income, tranche reconstruction) on a recorded fixture.

This also pins the highest-risk regression: interest income must survive
canonicalization (it would silently vanish without `TxnType.INTEREST`). Pure, no
network — the reader is a fake.
"""

from __future__ import annotations

import pandas as pd

from portfolio_analytics.broker_io.realized_income import build_realized_income
from portfolio_analytics.broker_io.transactions import reconstruct_tranches
from portfolio_analytics.domain.ledger import TxnType
from portfolio_analytics.ingestion.ingest import OwnershipPolicy, ingest
from portfolio_analytics.ingestion.reader_adapter import CanonicalReader
from portfolio_analytics.ingestion.snaptrade import SnapTradeConnector
from portfolio_analytics.ingestion.store import CanonicalStore


# ── fixture: a fake SnapTradeReader emitting SnapTrade-native shapes ──────────
def _sa(ttype, ticker, when, *, units=0.0, price=0.0, amount=0.0, fee=0.0, acct="U001", ccy="USD"):
    """A SnapTrade-native activity dict (nested symbol/currency)."""
    sym = {"symbol": ticker, "currency": {"code": ccy}} if ticker else None
    return {
        "type": ttype,
        "trade_date": when,
        "units": units,
        "price": price,
        "amount": amount,
        "fee": fee,
        "account_number": acct,
        "symbol": sym,
        "currency": {"code": ccy},
    }


_ACCOUNTS = [
    {
        "id": "uuid-1",
        "name": "Growth",
        "number": "U001",
        "type": "Individual",
        "balance_total": 10000.0,
        "last_holdings_sync": "2026-06-08T12:00:00+00:00",
        "institution": "Interactive Brokers",
    },
    {
        "id": "uuid-2",
        "name": "Roth",
        "number": "U002",
        "type": "Roth IRA",
        "balance_total": 5000.0,
        "last_holdings_sync": "2026-06-08T11:00:00+00:00",
        "institution": "Interactive Brokers",
    },
]

_HOLDINGS = pd.DataFrame(
    [
        {
            "account_id": "uuid-1",
            "ticker": "AAPL",
            "currency": "USD",
            "shares": 10.0,
            "avg_cost": 100.0,
            "current_price": 150.0,
            "market_value": 1500.0,
            "account_name": "Growth",
            "account_number": "U001",
            "account_type": "Individual",
        },
        {
            "account_id": "uuid-1",
            "ticker": "RKLB",
            "currency": "USD",
            "shares": 50.0,
            "avg_cost": 20.0,
            "current_price": 30.0,
            "market_value": 1500.0,
            "account_name": "Growth",
            "account_number": "U001",
            "account_type": "Individual",
        },
        {
            "account_id": "uuid-2",
            "ticker": "VOO",
            "currency": "USD",
            "shares": 5.0,
            "avg_cost": 400.0,
            "current_price": 450.0,
            "market_value": 2250.0,
            "account_name": "Roth",
            "account_number": "U002",
            "account_type": "Roth IRA",
        },
    ]
)

_ACTIVITIES = [
    _sa("BUY", "AAPL", "2025-01-15", units=10.0, price=100.0),
    _sa("SELL", "AAPL", "2026-02-01", units=4.0, price=150.0),
    _sa("DIVIDEND", "AAPL", "2025-06-01", amount=23.0),
    _sa("INTEREST", "", "2025-07-01", amount=5.0),
    _sa("MYSTERY_TYPE", "AAPL", "2025-08-01", amount=1.0),  # unmapped → dropped
]


class _FakeReader:
    def get_accounts(self):
        return _ACCOUNTS

    def get_all_holdings(self):
        return _HOLDINGS

    def get_all_activities(self):
        return _ACTIVITIES


def _adapter() -> CanonicalReader:
    store = ingest(
        [SnapTradeConnector(_FakeReader())],
        OwnershipPolicy(default_source="snaptrade"),
        store=CanonicalStore(),
        persist=False,
    )
    return CanonicalReader(store)


# ── connector normalization ───────────────────────────────────────────────────
def test_connector_normalizes_all_record_types():
    snap = SnapTradeConnector(_FakeReader()).sync()
    assert snap.error is None
    assert {a.number for a in snap.accounts} == {"U001", "U002"}
    assert len(snap.holdings) == 3
    assert {s.ticker for s in snap.securities} == {"AAPL", "RKLB", "VOO"}
    # 4 mappable activities; MYSTERY_TYPE dropped
    assert len(snap.activities) == 4
    assert any(a.type is TxnType.INTEREST for a in snap.activities)


def test_connector_preserves_broker_native_account_fields():
    snap = SnapTradeConnector(_FakeReader()).sync()
    growth = next(a for a in snap.accounts if a.number == "U001")
    assert growth.account_id == "uuid-1"
    assert growth.name == "Growth"
    assert growth.account_type == "Individual"
    assert growth.institution == "Interactive Brokers"


# ── tax_treatment derivation (metron-ops#194) ──────────────────────────────────
def test_connector_derives_tax_treatment_from_broker_type():
    """Both fixture accounts carry a recognized SnapTrade ``type`` — the connector
    must positively resolve tax_treatment from it, not leave it "" for account_meta's
    keyword fallback to guess at."""
    snap = SnapTradeConnector(_FakeReader()).sync()
    by_number = {a.number: a for a in snap.accounts}
    assert by_number["U001"].account_type == "Individual"
    assert by_number["U001"].tax_treatment == "taxable"
    assert by_number["U002"].account_type == "Roth IRA"
    assert by_number["U002"].tax_treatment == "tax_exempt"


def test_connector_leaves_tax_treatment_blank_for_unrecognized_broker_type():
    class _Reader(_FakeReader):
        def get_accounts(self):
            return [{**_ACCOUNTS[0], "type": "Some Exotic Wrapper"}]

    snap = SnapTradeConnector(_Reader()).sync()
    assert snap.accounts[0].tax_treatment == ""


def test_connector_fail_soft_on_reader_error():
    class _Boom:
        def get_accounts(self):
            raise RuntimeError("token expired")

    snap = SnapTradeConnector(_Boom()).sync()
    assert snap.error == "token expired"
    assert snap.accounts == []


# ── parity: adapter output == direct reader output ─────────────────────────────
def test_parity_get_accounts():
    direct = _FakeReader().get_accounts()
    via = _adapter().get_accounts()
    keyed = {a["number"]: a for a in via}
    for a in direct:
        assert keyed[a["number"]] == a  # exact dict equality, all keys


def test_parity_total_nav():
    assert _adapter().get_total_nav() == sum(a["balance_total"] for a in _ACCOUNTS)


def test_parity_aggregated_holdings():
    from portfolio_analytics.broker_io.snaptrade_reader import aggregate_holdings

    direct = aggregate_holdings(_FakeReader().get_all_holdings()).sort_values("ticker").reset_index(drop=True)
    via = _adapter().get_aggregated_holdings(None).sort_values("ticker").reset_index(drop=True)
    pd.testing.assert_frame_equal(direct[via.columns], via, check_like=True)


def test_parity_aggregated_holdings_account_subset():
    from portfolio_analytics.broker_io.snaptrade_reader import aggregate_holdings

    direct = aggregate_holdings(_FakeReader().get_all_holdings(), ["U001"]).sort_values("ticker").reset_index(drop=True)
    via = _adapter().get_aggregated_holdings(["U001"]).sort_values("ticker").reset_index(drop=True)
    pd.testing.assert_frame_equal(direct[via.columns], via, check_like=True)


def test_parity_realized_income_including_interest():
    direct = build_realized_income(_FakeReader().get_all_activities())
    via = build_realized_income(_adapter().get_all_activities())
    assert via == direct
    # the regression guard: interest income is present, not silently dropped
    y2025 = next(y for y in via["years"] if y.year == 2025)
    assert y2025.interest == 5.0
    assert y2025.dividends == 23.0


def test_parity_reconstruct_tranches():
    holdings = [{"ticker": "AAPL", "shares": 6.0, "avg_cost": 100.0, "currency": "USD"}]
    direct = reconstruct_tranches(_FakeReader().get_all_activities(), holdings)
    via = reconstruct_tranches(_adapter().get_all_activities(), holdings)
    assert via["AAPL"].reconstructed_shares == direct["AAPL"].reconstructed_shares
    assert [(lot.quantity, lot.cost_per_share) for lot in via["AAPL"].lots] == [
        (lot.quantity, lot.cost_per_share) for lot in direct["AAPL"].lots
    ]
