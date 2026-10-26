"""Account-basis inputs on the per-account snapshot (metron-ops#371, MET-03 slice 3).

The account engine needs each account's cash on the same day as its holdings NAV, and
where that cash came from. Per-account history accrues forward only, so the nightly
recorder writes both now; this file pins that, and pins how a stored row becomes an
engine observation (positions from the composition legs, NULL cash never read as 0).
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.db import models
from api.services import analytics
from api.services import performance as perf
from api.services.account_basis import observation_from_account_snapshot, positions_from_composition
from portfolio_analytics.domain import account_performance as ap

CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
)
DAY = date(2024, 1, 3)


@pytest.fixture()
def seeded(client):
    tenant = str(uuid.uuid4())
    hdr = {"X-Tenant-Id": tenant}
    pid = client.post("/portfolios", json={"name": "P"}, headers=hdr).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=hdr,
    )
    assert r.status_code == 200
    accounts = {a["name"]: a["account_id"] for a in client.get(f"/portfolios/{pid}/accounts", headers=hdr).json()}
    return uuid.UUID(tenant), uuid.UUID(pid), uuid.UUID(accounts["Brokerage"])


def _held(ticker: str = "AAPL", qty: float = 10.0, price: float = 110.0):
    return SimpleNamespace(
        ticker=ticker, quantity=qty, last_price=price, last_price_date=DAY, currency="USD",
        fx_rate=1.0, market_value=qty * price, cost_basis_base=1000.0, security_type="equity",
        last_price_from_close=True,
    )


def _record(session, monkeypatch, tid, pid, aid):
    monkeypatch.setattr(analytics, "valued_holdings_by_account", lambda *a, **k: {aid: [_held()]})
    assert perf.record_account_snapshots(session, tid, pid, today=DAY, source=lambda s: {}) == 1
    return session.scalars(
        select(models.AccountNavSnapshot).where(models.AccountNavSnapshot.account_id == aid)
    ).one()


def test_ledger_account_snapshot_records_ledger_cash(db_session, seeded, monkeypatch):
    tid, pid, aid = seeded
    expected = analytics._cash_by_account(db_session, tid, pid, account_ids=[aid])[aid]
    row = _record(db_session, monkeypatch, tid, pid, aid)
    assert float(row.nav) == pytest.approx(1100.0)
    assert row.cash_usd is not None and float(row.cash_usd) == pytest.approx(expected)
    assert row.cash_source == "ledger"


def test_snapshot_sourced_account_records_custodian_cash(db_session, seeded, monkeypatch):
    tid, pid, aid = seeded
    db_session.get(models.Account, aid).cash_balance_usd = 2500.0
    db_session.commit()
    monkeypatch.setattr(analytics, "_snapshot_sourced_account_ids", lambda *a, **k: {aid})
    row = _record(db_session, monkeypatch, tid, pid, aid)
    assert float(row.cash_usd) == pytest.approx(2500.0)
    assert row.cash_source == "custodian"


def test_unsynced_custodian_balance_stays_null_not_zero(db_session, seeded, monkeypatch):
    tid, pid, aid = seeded
    db_session.get(models.Account, aid).cash_balance_usd = None  # never synced a balance
    db_session.commit()
    monkeypatch.setattr(analytics, "_snapshot_sourced_account_ids", lambda *a, **k: {aid})
    row = _record(db_session, monkeypatch, tid, pid, aid)
    assert row.cash_usd is None and row.cash_source is None
    assert observation_from_account_snapshot(row, accrued_income=0.0, financing_debit=0.0).wealth is None


def test_positions_come_from_the_composition_legs():
    comp = {"schema": 1, "legs": [{"ticker": "AAPL", "qty": 10}, {"ticker": "MSFT", "qty": 2.5},
                                  {"ticker": "AAPL", "qty": 1}]}
    assert positions_from_composition(comp) == {"AAPL": 11.0, "MSFT": 2.5}
    assert positions_from_composition({"schema": 1, "legs": []}) == {}  # observed: holds nothing


@pytest.mark.parametrize("comp", [
    None,
    {},
    {"schema": 2, "legs": [{"ticker": "AAPL", "qty": 1}]},
    {"schema": 1},
    {"schema": 1, "legs": [{"ticker": "AAPL"}]},
])
def test_unreadable_composition_is_unobserved_positions(comp):
    assert positions_from_composition(comp) is None


def _row(day: date, nav: float, cash: float | None, *, qty: float = 10.0, source: str = "custodian",
         composition: bool = True):
    return SimpleNamespace(
        snap_date=day, nav=nav, cash_usd=cash, cash_source=source if cash is not None else None,
        composition={"schema": 1, "legs": [{"ticker": "AAPL", "qty": qty}]} if composition else None,
    )


def test_unobserved_accruals_are_never_zero_by_default():
    obs = observation_from_account_snapshot(_row(DAY, 1100.0, 50.0))
    assert obs.wealth is None and "accrued_income" in (obs.unavailable_reason or "")


def test_stored_rows_drive_the_engine_to_a_reconciled_period():
    a = observation_from_account_snapshot(_row(date(2024, 1, 3), 1100.0, 50.0),
                                          accrued_income=0.0, financing_debit=0.0)
    b = observation_from_account_snapshot(_row(date(2024, 1, 4), 1210.0, 50.0),
                                          accrued_income=0.0, financing_debit=0.0)
    p = ap.decompose_period(a, b, [])
    assert p.status == "reconciled" and p.net_pnl == pytest.approx(110.0)


def test_a_row_without_cash_or_composition_makes_the_period_unavailable():
    good = observation_from_account_snapshot(_row(date(2024, 1, 3), 1100.0, 50.0),
                                             accrued_income=0.0, financing_debit=0.0)
    no_cash = observation_from_account_snapshot(_row(date(2024, 1, 4), 1210.0, None),
                                                accrued_income=0.0, financing_debit=0.0)
    no_pos = observation_from_account_snapshot(_row(date(2024, 1, 4), 1210.0, 50.0, composition=False),
                                               accrued_income=0.0, financing_debit=0.0)
    assert ap.decompose_period(good, no_cash, []).status == "unavailable"
    assert ap.decompose_period(good, no_pos, []).status == "unavailable"


def test_an_unrecorded_position_jump_in_stored_rows_is_unreconciled():
    a = observation_from_account_snapshot(_row(date(2024, 1, 3), 1100.0, 50.0),
                                          accrued_income=0.0, financing_debit=0.0)
    b = observation_from_account_snapshot(_row(date(2024, 1, 4), 2200.0, 50.0, qty=20.0),
                                          accrued_income=0.0, financing_debit=0.0)
    assert ap.decompose_period(a, b, []).status == ap.UNRECONCILED
