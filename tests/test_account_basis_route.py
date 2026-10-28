"""``GET /portfolios/{id}/performance?basis=account`` (metron-ops#371, MET-03 slice 3b).

The account basis is opt-in. The default response must not change (the block is null),
and the opted-in block runs ``account_performance.v1`` per account over the stored
per-account snapshots and the account's ledger events, with its conventions declared.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from api.db import models

CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2024-01-01,DEPOSIT,,,,1000,Brokerage\n"
    "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-01-04,DIVIDEND,AAPL,,,5,Brokerage\n"
    "2024-01-04,DEPOSIT,,,,100,Brokerage\n"
    "2024-01-02,BUY,MSFT,10,200,2000,IRA\n"
)


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


@pytest.fixture()
def seeded(client):
    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    accounts = {
        a["name"]: a["account_id"]
        for a in client.get(f"/portfolios/{pid}/accounts", headers=_hdr(tenant)).json()
    }
    return tenant, pid, accounts


def _snap(session, tenant, pid, account_id, when, *, qty, price, cash, ticker="AAPL",
          cash_source="ledger", provisional=False, composition=True):
    session.add(
        models.AccountNavSnapshot(
            tenant_id=uuid.UUID(tenant),
            portfolio_id=uuid.UUID(pid),
            account_id=uuid.UUID(account_id),
            snap_date=when,
            nav=qty * price,
            cost_basis=1000.0,
            external_flow=0.0,
            provisional=provisional,
            composition=(
                {"schema": 1, "legs": [{"ticker": ticker, "qty": qty, "close": price}]}
                if composition else None
            ),
            cash_usd=cash,
            cash_source=cash_source if cash is not None else None,
        )
    )
    session.commit()


def _perf(client, tenant, pid, query=""):
    r = client.get(f"/portfolios/{pid}/performance{query}", headers=_hdr(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def _by_account(body):
    return {a["account_id"]: a for a in body["account_basis"]["accounts"]}


def test_default_response_has_no_account_basis(client, seeded):
    tenant, pid, _ = seeded
    assert _perf(client, tenant, pid)["account_basis"] is None
    assert _perf(client, tenant, pid, "?basis=holdings_sleeve")["account_basis"] is None


def test_unknown_basis_is_rejected(client, seeded):
    tenant, pid, _ = seeded
    r = client.get(f"/portfolios/{pid}/performance?basis=blended", headers=_hdr(tenant))
    assert r.status_code == 422


def test_account_basis_reconciles_dividend_and_deposit(client, db_session, seeded):
    """01-03: 10 AAPL at 110, cash 0. 01-04: 10 AAPL at 121, cash 105 (dividend 5 and a
    deposit of 100, both on 01-04). The deposit is external at the start of the day, the
    dividend is internal income, so TWR = (1210 + 105) / (1100 + 100) − 1."""
    tenant, pid, acct = seeded
    b = acct["Brokerage"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0)

    body = _perf(client, tenant, pid, f"?basis=account&account_id={b}")
    block = body["account_basis"]
    assert block["contract"] == "account_performance.v1"
    assert block["basis"] == "account"
    assert {"income_recognition", "financing", "external_capital"} <= set(block["conventions"])
    (row,) = block["accounts"]
    assert row["account_id"] == b
    assert (row["n_snapshots"], row["periods_reconciled"], row["periods_unreconciled"]) == (2, 1, 0)
    m = {x["name"]: x for x in row["metrics"]}
    assert m["twr_net"]["value"] == pytest.approx(1315.0 / 1200.0 - 1.0)
    assert m["twr_net"]["basis"] == "account" and m["twr_net"]["units"] == "fraction"
    assert m["net_pnl"]["value"] == pytest.approx(115.0)  # 110 price + 5 income
    assert m["net_pnl"]["unavailable_reason"] is None
    # The holdings-sleeve fields beside it are untouched by the opt-in.
    assert body["n_snapshots"] == _perf(client, tenant, pid, f"?account_id={b}")["n_snapshots"]


def test_unexplained_position_change_is_unreconciled_not_return(client, db_session, seeded):
    tenant, pid, acct = seeded
    b = acct["Brokerage"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    # 5 shares arrive with no ledger event (a transfer in kind): not investment return.
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=15, price=110.0, cash=105.0)
    (row,) = _perf(client, tenant, pid, f"?basis=account&account_id={b}")["account_basis"]["accounts"]
    assert row["periods_unreconciled"] == 1
    twr = next(x for x in row["metrics"] if x["name"] == "twr_net")
    assert twr["value"] is None
    assert "unreconciled" in twr["unavailable_reason"] and "AAPL" in twr["unavailable_reason"]


def test_unobserved_cash_is_unavailable_never_zero(client, db_session, seeded):
    tenant, pid, acct = seeded
    b = acct["Brokerage"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=None)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0)
    (row,) = _perf(client, tenant, pid, f"?basis=account&account_id={b}")["account_basis"]["accounts"]
    assert row["periods_unavailable"] == 1
    twr = next(x for x in row["metrics"] if x["name"] == "twr_net")
    assert twr["value"] is None and "cash not observed" in twr["unavailable_reason"]


def test_provisional_rows_excluded_and_short_history_named(client, db_session, seeded):
    tenant, pid, acct = seeded
    b, ira = acct["Brokerage"], acct["IRA"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0,
          provisional=True)
    rows = _by_account(_perf(client, tenant, pid, "?basis=account"))
    # Whole-portfolio scope lists every account, including one with no snapshots at all.
    assert set(rows) == {b, ira}
    assert rows[b]["n_snapshots"] == 1 and rows[b]["n_provisional_excluded"] == 1
    assert rows[b]["metrics"] == [] and "needs two" in rows[b]["unavailable_reason"]
    assert rows[ira]["n_snapshots"] == 0 and rows[ira]["first_date"] is None


def test_selection_scopes_the_accounts(client, db_session, seeded):
    tenant, pid, acct = seeded
    rows = _by_account(_perf(client, tenant, pid, f"?basis=account&account_id={acct['IRA']}"))
    assert set(rows) == {acct["IRA"]}


# --- Slice 3c: the scope as one book ---------------------------------------------------


def _rollup_metrics(block):
    return {x["name"]: x for x in block["rollup"]["metrics"]}


def test_rollup_sums_accounts_and_keeps_the_scope_boundary(client, db_session, seeded):
    """Brokerage as in the dividend test; the IRA holds 10 MSFT, 200 -> 210, no cash.
    Rollup wealth 3100 -> 3415 with the 100 deposit external: TWR = 3415 / 3200 − 1,
    net P&L = 110 + 100 price + 5 dividend."""
    tenant, pid, acct = seeded
    b, ira = acct["Brokerage"], acct["IRA"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0)
    _snap(db_session, tenant, pid, ira, date(2024, 1, 3), qty=10, price=200.0, cash=0.0, ticker="MSFT")
    _snap(db_session, tenant, pid, ira, date(2024, 1, 4), qty=10, price=210.0, cash=0.0, ticker="MSFT")

    block = _perf(client, tenant, pid, "?basis=account")["account_basis"]
    assert "rollup" in block["conventions"]
    r = block["rollup"]
    assert set(r["account_ids"]) == {b, ira}
    assert (r["n_dates"], r["n_dates_skipped"], r["periods_reconciled"]) == (2, 0, 1)
    assert (r["first_date"], r["last_date"]) == ("2024-01-03", "2024-01-04")
    m = _rollup_metrics(block)
    assert m["twr_net"]["value"] == pytest.approx(3415.0 / 3200.0 - 1.0)
    assert m["net_pnl"]["value"] == pytest.approx(215.0)
    assert m["twr_net"]["basis"] == "account" and m["twr_net"]["unavailable_reason"] is None


def test_rollup_uses_only_dates_every_account_has(client, db_session, seeded):
    tenant, pid, acct = seeded
    b, ira = acct["Brokerage"], acct["IRA"]
    for day, price, cash in ((3, 110.0, 0.0), (4, 121.0, 105.0), (5, 121.0, 105.0)):
        _snap(db_session, tenant, pid, b, date(2024, 1, day), qty=10, price=price, cash=cash)
    for day in (3, 5):
        _snap(db_session, tenant, pid, ira, date(2024, 1, day), qty=10, price=200.0, cash=0.0, ticker="MSFT")
    r = _perf(client, tenant, pid, "?basis=account")["account_basis"]["rollup"]
    assert (r["n_dates"], r["n_dates_skipped"], r["periods_reconciled"]) == (2, 1, 1)
    assert (r["first_date"], r["last_date"]) == ("2024-01-03", "2024-01-05")


def test_rollup_never_drops_an_account_without_snapshots(client, db_session, seeded):
    tenant, pid, acct = seeded
    b = acct["Brokerage"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0)
    r = _perf(client, tenant, pid, "?basis=account")["account_basis"]["rollup"]
    assert r["metrics"] == [] and r["n_dates"] == 0 and r["n_dates_skipped"] == 2
    assert acct["IRA"] in r["unavailable_reason"] and "left out of the sum" in r["unavailable_reason"]
    # Scoped to the one account that has rows, the rollup is that account.
    one = _perf(client, tenant, pid, f"?basis=account&account_id={b}")["account_basis"]
    assert _rollup_metrics(one)["twr_net"]["value"] == pytest.approx(1315.0 / 1200.0 - 1.0)


def test_rollup_needs_two_common_dates(client, db_session, seeded):
    tenant, pid, acct = seeded
    b, ira = acct["Brokerage"], acct["IRA"]
    _snap(db_session, tenant, pid, b, date(2024, 1, 3), qty=10, price=110.0, cash=0.0)
    _snap(db_session, tenant, pid, b, date(2024, 1, 4), qty=10, price=121.0, cash=105.0)
    _snap(db_session, tenant, pid, ira, date(2024, 1, 4), qty=10, price=200.0, cash=0.0, ticker="MSFT")
    r = _perf(client, tenant, pid, "?basis=account")["account_basis"]["rollup"]
    assert r["n_dates"] == 1 and r["first_date"] == "2024-01-04"
    assert "needs two" in r["unavailable_reason"] and r["metrics"] == []


def test_rollup_is_null_for_a_portfolio_with_no_accounts(client):
    tenant = str(uuid.uuid4())
    pid = client.post("/portfolios", json={"name": "Empty"}, headers=_hdr(tenant)).json()["id"]
    block = _perf(client, tenant, pid, "?basis=account")["account_basis"]
    assert block["accounts"] == [] and block["rollup"] is None
