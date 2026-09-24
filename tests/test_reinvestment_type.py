"""First-class dividend-reinvestment transaction type, end to end (metron-ops#335).

A reinvested dividend (DRP/DRIP) is ``TxnType.REINVESTMENT``: economically a BUY funded
by a dividend, so every layer that asks "is this a purchase" must answer yes for it and
compute exactly what it would for a BUY. These tests hold that at each layer — ledger,
flow arithmetic, ingestion (CSV / OFX / SnapTrade round trip / canonical store),
persistence (including re-typing rows stored as BUY before the type existed) and the
API — so the type can only ever change how a row READS, never a number. The demo
household's TWR / attribution goldens are pinned in ``tests/test_demo_household.py::
TestReinvestmentType``.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import replace
from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from api.db import models
from api.services import analytics, compute_cache
from api.services import performance as perf
from api.services.persistence import persist_snapshot
from api.services.shadow_recompute import _txn_flow
from portfolio_analytics.broker_io.csv_import import parse_transactions_csv
from portfolio_analytics.broker_io.transactions import activities_to_transactions
from portfolio_analytics.domain import PURCHASE_TYPES
from portfolio_analytics.domain.ledger import Ledger, Lot, Transaction, TxnType, _apply, build_ledger
from portfolio_analytics.ingestion import CanonicalReader, CanonicalStore, activity_key, legacy_activity_key
from portfolio_analytics.ingestion.schema import CanonicalActivity
from portfolio_analytics.ingestion.store import load_store, save_store

D = date(2024, 3, 1)


def _txn(type_: TxnType, **kw) -> Transaction:
    return Transaction(when=kw.pop("when", D), type=type_, ticker=kw.pop("ticker", "KO"), **kw)


# ── Ledger ────────────────────────────────────────────────────────────────────────


class TestLedger:
    def test_purchase_types_are_exactly_buy_and_reinvestment(self):
        assert PURCHASE_TYPES == {TxnType.BUY, TxnType.REINVESTMENT}
        assert TxnType.REINVESTMENT.value == "REINVESTMENT"
        for t in TxnType:
            assert t.is_purchase is (t in PURCHASE_TYPES)

    @pytest.mark.parametrize(
        "reinvest",
        [
            dict(quantity=0.4, price=50.0, amount=20.0, fees=0.0),
            dict(quantity=0.4, price=50.0, amount=20.0, fees=0.25),
            dict(quantity=0.4, price=0.0, amount=20.0),  # price missing → cost from amount
        ],
    )
    def test_reinvestment_ledger_is_identical_to_the_same_row_as_a_buy(self, reinvest):
        history = [
            _txn(TxnType.BUY, when=date(2024, 1, 2), quantity=10, price=50.0),
            _txn(TxnType.DIVIDEND, amount=20.0),
            _txn(TxnType.REINVESTMENT, **reinvest),
            _txn(TxnType.SELL, when=date(2024, 6, 3), quantity=10.2, price=60.0),
        ]
        as_buys = [replace(t, type=TxnType.BUY) if t.type is TxnType.REINVESTMENT else t for t in history]
        got, want = build_ledger(history), build_ledger(as_buys)
        assert got.cash == want.cash
        assert got.realized == want.realized
        assert got.position("KO") == want.position("KO")
        assert got.open_lots == want.open_lots

    def test_reinvestment_opens_its_own_lot(self):
        ledger = build_ledger([_txn(TxnType.REINVESTMENT, quantity=0.4, price=50.0, amount=20.0)])
        assert [(lot.open_date, lot.quantity, lot.cost_per_share) for lot in ledger.open_lots["KO"]] == [
            (D, 0.4, 50.0)
        ]
        assert ledger.cash == pytest.approx(-20.0)

    @pytest.mark.parametrize("txn_type", list(TxnType))
    def test_every_type_has_a_ledger_branch(self, txn_type):
        """No member may fall through ``_apply`` silently — a type added without a
        branch would be a no-op here and fail this test."""

        def fresh() -> Ledger:
            return Ledger(open_lots={"KO": [Lot("KO", date(2024, 1, 2), 10.0, 50.0)]})

        def state(ledger: Ledger):
            return ledger.cash, [(lot.quantity, lot.cost_per_share) for lot in ledger.open_lots.get("KO", [])]

        ledger = fresh()
        _apply(ledger, _txn(txn_type, quantity=2.0, price=10.0, amount=5.0))
        assert state(ledger) != state(fresh())


# ── Flow arithmetic (TWR / shadow recompute / bond normalization) ─────────────────


class TestFlows:
    def test_purchase_flow_counts_a_reinvestment_as_a_buy(self):
        rows = [("BUY", 100.0), ("REINVESTMENT", 20.0), ("SELL", 30.0), ("DIVIDEND", 20.0), ("DEPOSIT", 500.0)]
        assert perf._purchase_flow(rows) == pytest.approx(90.0)
        assert perf._NET_PURCHASE_TYPE_VALUES == ["BUY", "REINVESTMENT", "SELL"]

    def test_shadow_flow_is_the_same_as_a_buy(self):
        for kw in (dict(quantity=0.4, price=50.0, amount=20.0), dict(quantity=0.4, price=0.0, amount=20.0)):
            assert _txn_flow(_txn(TxnType.REINVESTMENT, **kw)) == _txn_flow(_txn(TxnType.BUY, **kw)) == 20.0

    def test_bond_par_normalization_applies_to_a_reinvestment(self):
        assert analytics._normalize_bond_quantity("REINVESTMENT", 10000.0, 97.0, 9700.0) == 100.0
        assert analytics._normalize_bond_quantity("BUY", 10000.0, 97.0, 9700.0) == 100.0
        assert analytics._normalize_bond_quantity("DIVIDEND", 10000.0, 97.0, 9700.0) == 10000.0
        assert analytics._normalize_bond_quantity("REINVESTMENT", 0.4, 50.0, 20.0) == 0.4  # equity: untouched


# ── Ingestion ─────────────────────────────────────────────────────────────────────


def _act(type_: TxnType, **kw) -> CanonicalActivity:
    base = dict(account_number="A1", when=D, type=type_, security_id="EQ:KO:USD", quantity=0.4, price=50.0, amount=20.0)
    base.update(kw)
    return CanonicalActivity(**base)


class TestIngestion:
    def test_snaptrade_dict_round_trip_keeps_a_reinvestment(self):
        """``CanonicalReader`` round-trips canonical activities back through the
        SnapTrade dict shape; the canonical value must map back, not be dropped."""
        store = CanonicalStore()
        store.merge("csv", [], [], [], [_act(TxnType.REINVESTMENT)], [])
        txns = activities_to_transactions(CanonicalReader(store).get_all_activities())
        assert [(t.type, t.quantity) for t in txns] == [(TxnType.REINVESTMENT, 0.4)]

    def test_legacy_key_is_the_buy_typed_key_and_only_for_reinvestment(self):
        drp = _act(TxnType.REINVESTMENT)
        assert legacy_activity_key(drp) == activity_key(_act(TxnType.BUY))
        assert legacy_activity_key(drp) != activity_key(drp)
        for t in TxnType:
            if t is not TxnType.REINVESTMENT:
                assert legacy_activity_key(_act(t)) is None

    def test_store_merge_upgrades_a_legacy_buy_in_place(self, tmp_path):
        store = CanonicalStore()
        store.merge("csv", [], [], [], [_act(TxnType.BUY)], [])  # as stored before #335
        store.merge("csv", [], [], [], [_act(TxnType.REINVESTMENT)], [])
        store.merge("csv", [], [], [], [_act(TxnType.REINVESTMENT)], [])  # idempotent
        assert [a.type for a in store.all_activities()] == [TxnType.REINVESTMENT]
        # And it survives the silver-store JSON round trip.
        path = tmp_path / "silver.json"
        save_store(store, path)
        assert [a.type for a in load_store(path).all_activities()] == [TxnType.REINVESTMENT]

    def test_store_merge_keeps_a_distinct_buy_beside_a_reinvestment(self):
        store = CanonicalStore()
        store.merge("csv", [], [], [], [_act(TxnType.BUY, quantity=5.0, amount=250.0)], [])
        store.merge("csv", [], [], [], [_act(TxnType.REINVESTMENT)], [])
        assert sorted(a.type for a in store.all_activities()) == [TxnType.BUY, TxnType.REINVESTMENT]


# ── Persistence ───────────────────────────────────────────────────────────────────

CSV_DRP = """date,type,symbol,quantity,price,amount
2024-01-02,BUY,KO,10,50,500
2024-03-01,DIVIDEND,KO,,,20
2024-03-01,Reinvestment,KO,0.4,50,20
"""


def _make_portfolio(session):
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="P")
    session.add_all([tenant, portfolio])
    session.commit()
    return tenant.id, portfolio.id


def _legacy_snapshot():
    """The DRP CSV as it was ingested before #335: the reinvestment as a plain BUY."""
    snap = parse_transactions_csv(CSV_DRP).snapshot
    acts = [replace(a, type=TxnType.BUY) if a.type is TxnType.REINVESTMENT else a for a in snap.activities]
    return replace(snap, activities=acts)


def _types(session) -> list[str]:
    return sorted(session.scalars(select(models.Transaction.txn_type)).all())


class TestPersistence:
    def test_new_import_stores_reinvestment(self, db_session):
        tenant_id, pid = _make_portfolio(db_session)
        result = persist_snapshot(
            db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=parse_transactions_csv(CSV_DRP).snapshot
        )
        assert result.transactions_inserted == 3 and result.transactions_retyped == 0
        assert _types(db_session) == ["BUY", "DIVIDEND", "REINVESTMENT"]

    def test_reimport_retypes_a_legacy_buy_in_place_never_duplicates(self, db_session):
        tenant_id, pid = _make_portfolio(db_session)
        persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=_legacy_snapshot())
        assert _types(db_session) == ["BUY", "BUY", "DIVIDEND"]
        # Age the legacy rows so the fingerprint's max(created_at) is observably moved.
        for row in db_session.scalars(select(models.Transaction)).all():
            row.created_at = datetime(2020, 1, 1)
        db_session.commit()
        fp_before = compute_cache.portfolio_fingerprint(db_session, tenant_id, pid)
        shares_before = analytics.holdings(db_session, tenant_id, pid)[0].quantity

        snap = parse_transactions_csv(CSV_DRP).snapshot
        result = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=snap)

        assert (result.transactions_inserted, result.transactions_skipped, result.transactions_retyped) == (0, 2, 1)
        assert _types(db_session) == ["BUY", "DIVIDEND", "REINVESTMENT"]
        row = db_session.scalars(
            select(models.Transaction).where(models.Transaction.txn_type == "REINVESTMENT")
        ).one()
        assert "|REINVESTMENT|" in row.source_key
        # The type is the only thing that changed: same shares, and a cache that notices.
        compute_cache.clear()
        assert analytics.holdings(db_session, tenant_id, pid)[0].quantity == pytest.approx(shares_before)
        assert compute_cache.portfolio_fingerprint(db_session, tenant_id, pid) != fp_before

        again = persist_snapshot(db_session, tenant_id=tenant_id, portfolio_id=pid, snapshot=snap)
        assert (again.transactions_inserted, again.transactions_skipped, again.transactions_retyped) == (0, 3, 0)
        assert db_session.scalar(select(func.count(models.Transaction.id))) == 3

    def test_net_purchases_and_holdings_match_the_all_buy_ledger(self, db_session):
        """The same history with the DRP stored as REINVESTMENT vs as BUY produces the
        same TWR flow input and the same position — the type is display-only."""
        t_new, p_new = _make_portfolio(db_session)
        t_old, p_old = _make_portfolio(db_session)
        persist_snapshot(db_session, tenant_id=t_new, portfolio_id=p_new, snapshot=parse_transactions_csv(CSV_DRP).snapshot)
        persist_snapshot(db_session, tenant_id=t_old, portfolio_id=p_old, snapshot=_legacy_snapshot())
        through = date(2024, 12, 31)
        new_flow = perf._net_purchases(db_session, t_new, p_new, after=None, through=through)
        old_flow = perf._net_purchases(db_session, t_old, p_old, after=None, through=through)
        assert new_flow == pytest.approx(old_flow) == pytest.approx(520.0)
        new_h = analytics.holdings(db_session, t_new, p_new)[0]
        old_h = analytics.holdings(db_session, t_old, p_old)[0]
        assert (new_h.quantity, new_h.cost_basis) == pytest.approx((old_h.quantity, old_h.cost_basis))
        assert new_h.quantity == pytest.approx(10.4)


# ── API ───────────────────────────────────────────────────────────────────────────


class TestApi:
    def test_import_and_transactions_endpoint_carry_reinvestment(self, client):
        headers = {"X-Tenant-Id": str(uuid.uuid4())}
        pid = client.post("/portfolios", json={"name": "P"}, headers=headers).json()["id"]
        r = client.post(
            f"/portfolios/{pid}/import/csv",
            files={"file": ("t.csv", io.BytesIO(CSV_DRP.encode()), "text/csv")},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["transactions_retyped"] == 0
        rows = client.get(f"/portfolios/{pid}/transactions", headers=headers).json()
        assert sorted(t["txn_type"] for t in rows) == ["BUY", "DIVIDEND", "REINVESTMENT"]

    def test_openapi_publishes_the_type_as_an_enum(self):
        from api.main import app

        spec = app.openapi()
        txn_type = spec["components"]["schemas"]["TransactionOut"]["properties"]["txn_type"]
        ref = txn_type.get("$ref", "").rsplit("/", 1)[-1]
        enum = spec["components"]["schemas"][ref]["enum"] if ref else txn_type["enum"]
        assert set(enum) == {t.value for t in TxnType}
        assert "REINVESTMENT" in enum
