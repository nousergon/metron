"""Security transfers in kind as canonical ledger members (metron-ops#378).

A position that arrives by ACATS, a journal between accounts or a transfer from another
broker is external capital at its transfer-date market value. It is not a purchase and
it is not return. These tests hold that at each layer: the ledger (lots carry their
basis, no cash moves, no gain is realized), the account basis (external capital, the
position gate is satisfied), the holdings sleeve and shadow flows, the quality gate, and
CSV import through persistence. Every figure here is synthetic.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.db import models
from api.services import analytics
from api.services import performance as perf
from api.services.persistence import persist_snapshot
from api.services.shadow_recompute import _txn_flow
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.domain import POSITION_SIGN, PURCHASE_TYPES, TRANSFER_TYPES
from portfolio_analytics.domain import account_performance as ap
from portfolio_analytics.domain.account_performance import EventKind, WealthObservation
from portfolio_analytics.domain.ledger import Transaction, TxnType, build_ledger, external_cash_flows
from portfolio_analytics.ingestion.quality import check_snapshot_contract

D0 = date(2025, 3, 3)


def d(i: int) -> date:
    return D0 + timedelta(days=i)


def _txn(type_: TxnType, i: int = 0, **kw) -> Transaction:
    return Transaction(when=d(i), type=type_, ticker=kw.pop("ticker", "XYZ"), **kw)


# ── Vocabulary ────────────────────────────────────────────────────────────────────


class TestVocabulary:
    def test_transfers_are_their_own_members_and_never_purchases(self):
        assert TRANSFER_TYPES == {TxnType.TRANSFER_IN, TxnType.TRANSFER_OUT}
        assert not TRANSFER_TYPES & PURCHASE_TYPES
        assert not TxnType.TRANSFER_IN.is_purchase

    def test_position_sign_covers_every_share_moving_type(self):
        assert POSITION_SIGN == {
            TxnType.BUY: 1.0,
            TxnType.REINVESTMENT: 1.0,
            TxnType.TRANSFER_IN: 1.0,
            TxnType.SELL: -1.0,
            TxnType.TRANSFER_OUT: -1.0,
        }
        # The sleeve's flow set and the signed-quantity map name the same types, so
        # snapshot_gap can sign every row the flow query returns.
        assert set(perf._NET_PURCHASE_TYPE_VALUES) == {t.value for t in POSITION_SIGN}

    def test_values_fit_the_stored_column(self):
        assert all(len(t.value) <= 20 for t in TxnType)  # models.Transaction.txn_type is String(20)


# ── Ledger ────────────────────────────────────────────────────────────────────────


class TestLedger:
    def test_transfer_in_carries_the_custodian_basis_and_moves_no_cash(self):
        ledger = build_ledger([_txn(TxnType.TRANSFER_IN, quantity=100, price=40.0, amount=10_000.0)])
        (lot,) = ledger.open_lots["XYZ"]
        assert (lot.quantity, lot.cost_per_share, lot.open_date) == (100, 40.0, D0)
        assert ledger.cash == 0.0
        assert ledger.realized == []

    def test_transfer_in_without_a_basis_falls_back_to_transfer_date_value(self):
        ledger = build_ledger([_txn(TxnType.TRANSFER_IN, quantity=100, amount=10_000.0)])
        assert ledger.position("XYZ") == pytest.approx((100.0, 100.0))

    def test_transfer_in_with_neither_basis_nor_value_still_opens_the_shares(self):
        ledger = build_ledger([_txn(TxnType.TRANSFER_IN, quantity=5)])
        (lot,) = ledger.open_lots["XYZ"]
        assert (lot.quantity, lot.cost_per_share) == (5, 0.0)

    def test_transfer_out_relieves_fifo_without_realizing_or_moving_cash(self):
        ledger = build_ledger([
            _txn(TxnType.BUY, 0, quantity=10, price=50.0),
            _txn(TxnType.BUY, 1, quantity=10, price=60.0),
            _txn(TxnType.TRANSFER_OUT, 2, quantity=15, amount=1_000.0),
        ])
        (lot,) = ledger.open_lots["XYZ"]
        assert (lot.quantity, lot.cost_per_share) == (5, 60.0)
        assert ledger.realized == []
        assert ledger.cash == pytest.approx(-1_100.0)  # the two buys only

    def test_a_transferred_in_position_can_later_be_sold(self):
        """Before #378 a sale of shares that arrived by transfer exceeded every BUY the
        ledger could see, and the ticker was dropped as incomplete history."""
        ledger = build_ledger([
            _txn(TxnType.TRANSFER_IN, 0, quantity=10, price=20.0, amount=300.0),
            _txn(TxnType.SELL, 5, quantity=4, price=35.0),
        ])
        (gain,) = ledger.realized
        assert (gain.quantity, gain.cost_basis, gain.proceeds) == pytest.approx((4, 80.0, 140.0))
        assert ledger.position("XYZ")[0] == pytest.approx(6)

    def test_transfer_out_beyond_holdings_fails_loud_like_a_sell(self):
        with pytest.raises(ValueError, match="TRANSFER_OUT of 3"):
            build_ledger([_txn(TxnType.TRANSFER_OUT, quantity=3, amount=30.0)])

    @pytest.mark.parametrize("txn_type", sorted(TRANSFER_TYPES))
    def test_non_positive_quantity_is_skipped(self, txn_type):
        ledger = build_ledger([_txn(txn_type, quantity=0, amount=10.0)])
        assert ledger.open_lots == {} and ledger.cash == 0.0

    def test_external_cash_flows_include_transfers_at_market_value(self):
        flows = external_cash_flows([
            _txn(TxnType.DEPOSIT, 0, amount=500.0, ticker=""),
            _txn(TxnType.TRANSFER_IN, 1, quantity=10, amount=1_000.0),
            _txn(TxnType.TRANSFER_OUT, 2, quantity=2, amount=250.0),
            _txn(TxnType.BUY, 3, quantity=1, price=10.0),
        ])
        assert [(f.when, f.amount) for f in flows] == [(d(0), -500.0), (d(1), -1_000.0), (d(2), 250.0)]


# ── Account basis ─────────────────────────────────────────────────────────────────


def _obs(i: int, mv: float, cash: float, pos: dict[str, float]) -> WealthObservation:
    return WealthObservation(d(i), mv, cash, "custodian", 0.0, 0.0, positions=pos)


class TestAccountBasis:
    def test_from_transaction_maps_transfers_at_market_value(self):
        e_in = ap.from_transaction(_txn(TxnType.TRANSFER_IN, quantity=100, price=40.0, amount=10_000.0))
        e_out = ap.from_transaction(_txn(TxnType.TRANSFER_OUT, quantity=30, amount=3_300.0))
        assert (e_in.kind, e_in.amount, e_in.symbol, e_in.quantity) == (EventKind.TRANSFER_IN, 10_000.0, "XYZ", 100)
        assert (e_out.kind, e_out.amount, e_out.quantity) == (EventKind.TRANSFER_OUT, 3_300.0, 30)
        assert ap.account_external_flow(e_in) == 10_000.0
        assert ap.account_external_flow(e_out) == -3_300.0

    def test_every_ledger_type_maps_onto_the_account_vocabulary(self):
        for t in TxnType:
            ap.from_transaction(_txn(t, quantity=1, price=1.0, amount=1.0))  # KeyError if unmapped

    def test_a_transferred_in_position_is_external_capital_not_return(self):
        # 1,000 of cash; 100 shares worth 10,000 arrive by transfer and gain 50 by the close.
        events = [ap.from_transaction(_txn(TxnType.TRANSFER_IN, 1, quantity=100, amount=10_000.0))]
        result = ap.account_performance(
            [_obs(0, 0.0, 1_000.0, {}), _obs(1, 10_050.0, 1_000.0, {"XYZ": 100})], events
        )
        (p,) = result.periods
        assert p.status == "reconciled"
        assert p.external == pytest.approx(10_000.0)
        assert p.price_pnl == pytest.approx(50.0)
        assert p.net_pnl == pytest.approx(50.0)

    def test_a_transfer_out_is_external_capital_leaving(self):
        events = [ap.from_transaction(_txn(TxnType.TRANSFER_OUT, 1, quantity=40, amount=4_000.0))]
        result = ap.account_performance(
            [_obs(0, 10_000.0, 0.0, {"XYZ": 100}), _obs(1, 6_000.0, 0.0, {"XYZ": 60})], events
        )
        (p,) = result.periods
        assert p.status == "reconciled"
        assert p.external == pytest.approx(-4_000.0)
        assert p.net_pnl == pytest.approx(0.0)

    def test_an_unvalued_transfer_leaves_the_period_unreconciled(self):
        """No transfer-date value means no external capital is known. Mapping the shares
        anyway would let the gate pass and the whole position read as return."""
        events = [ap.from_transaction(_txn(TxnType.TRANSFER_IN, 1, quantity=100, amount=0.0))]
        assert events[0].quantity == 0.0 and events[0].amount == 0.0
        result = ap.account_performance(
            [_obs(0, 0.0, 1_000.0, {}), _obs(1, 10_000.0, 1_000.0, {"XYZ": 100})], events
        )
        (p,) = result.periods
        assert p.status == ap.UNRECONCILED and p.net_pnl is None
        assert "XYZ +100" in (p.unavailable_reason or "")


# ── Holdings sleeve and shadow recompute ──────────────────────────────────────────


class TestSleeveFlows:
    def test_purchase_flow_counts_transfers_as_capital_in_and_out(self):
        rows = [("BUY", 100.0), ("TRANSFER_IN", 10_000.0), ("SELL", 30.0), ("TRANSFER_OUT", 2_000.0)]
        assert perf._purchase_flow(rows) == pytest.approx(8_070.0)

    def test_shadow_flow_matches_the_sleeve_convention(self):
        assert _txn_flow(_txn(TxnType.TRANSFER_IN, quantity=100, price=40.0, amount=10_000.0)) == 10_000.0
        assert _txn_flow(_txn(TxnType.TRANSFER_OUT, quantity=10, amount=1_100.0)) == -1_100.0


# ── Ingestion: quality gate, CSV, persistence ─────────────────────────────────────

CSV_TRANSFER = """date,type,symbol,quantity,price,amount
2025-03-03,TRANSFER_IN,XYZ,100,40,10000
2025-04-01,SELL,XYZ,10,120,1200
2025-05-01,transfer_out,XYZ,20,0,2600
"""


def _make_portfolio(session):
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="P")
    session.add_all([tenant, portfolio])
    session.commit()
    return tenant.id, portfolio.id


class TestIngestion:
    def test_csv_accepts_canonical_transfer_types(self):
        result = parse_transactions_csv(CSV_TRANSFER)
        assert result.errors == []
        assert [a.type for a in result.snapshot.activities] == [
            TxnType.TRANSFER_IN, TxnType.SELL, TxnType.TRANSFER_OUT,
        ]

    def test_csv_transfer_without_a_symbol_is_an_error(self):
        result = parse_transactions_csv("date,type,symbol,quantity,amount\n2025-03-03,TRANSFER_IN,,5,500\n")
        assert result.snapshot.activities == []
        assert len(result.errors) == 1

    def test_quality_flags_a_transfer_with_no_market_value(self):
        snap = parse_transactions_csv("date,type,symbol,quantity,price,amount\n2025-03-03,TRANSFER_IN,XYZ,5,10,0\n").snapshot
        reasons = [f.detail for f in check_snapshot_contract(snap)]
        assert any("no transfer-date market value" in r for r in reasons)
        clean = parse_transactions_csv(CSV_TRANSFER).snapshot
        assert not any("TRANSFER" in f.detail for f in check_snapshot_contract(clean))

    def test_persisted_transfers_drive_holdings_and_the_sleeve_flow(self, db_session):
        tenant_id, pid = _make_portfolio(db_session)
        persist_snapshot(
            db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=parse_transactions_csv(CSV_TRANSFER).snapshot
        )
        (h,) = analytics.holdings(db_session, tenant_id, pid)
        assert h.quantity == pytest.approx(70)
        assert h.cost_basis == pytest.approx(70 * 40.0)  # the carried basis survives the move
        flow = perf._net_purchases(db_session, tenant_id, pid, after=None, through=date(2025, 12, 31))
        assert flow == pytest.approx(10_000.0 - 1_200.0 - 2_600.0)
