"""Statement budget for ``GET /portfolios/{id}/accounts`` — the "Balance by account" panel.

After metron#515 cut the rest of the Holdings landing page, this was the largest cold cost
left: ~25 statements PER ACCOUNT. The panel is a fan-out over accounts, and three reads
sat inside that loop:

* ``valued_holdings_by_account`` called ``holdings`` once per account (snapshot-account set,
  that account's ledger, positions, currency lookup: ~8 statements each);
* ``account_performance_series`` called ``_reconstruct_nav_points`` once per account, and
  each call re-read its lots, lot flows, holdings valuation, FX history and trade-ledger
  flow (~15 statements each), plus a forward-NAV read for any account without a line;
* the route valued every account again for the panel rows, again for the YTD/LTM series and
  again for the Day legs.

Measured on the 3-account seed below (SQLite, statements counted at the engine): 79 cold
before, 36 after; with four more accounts 175 / 111 before, 36 after. These tests pin the
shape — a count that does not depend on the number of accounts — not just today's number.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from api.db import models
from api.services import analytics, compute_cache
from tests.test_landing_page_statement_budget import TODAY, _seed, _seed_reference


def _add_accounts(session, tenant_id, pid, secs, n: int, kind: str) -> None:
    """``n`` more accounts of one kind: ``csv`` (ledger-sourced, one BUY) or ``snaptrade``
    (snapshot-sourced, one position and a connector cash balance)."""
    for i in range(n):
        acct = models.Account(
            tenant_id=tenant_id, portfolio_id=pid, broker=kind, external_id=f"X-{kind}-{i}",
            name=f"X{i}", currency="USD", cash_balance_usd=None if kind == "csv" else 10.0,
        )
        session.add(acct)
        session.flush()
        if kind == "csv":
            session.add(models.Transaction(
                tenant_id=tenant_id, account_id=acct.id, security_id=secs["CCC"].id, txn_type="BUY",
                quantity=2, price=100.0, amount=200.0, currency="USD",
                trade_date=TODAY - timedelta(days=100), source_key=f"x-{kind}-{i}",
            ))
        else:
            session.add(models.Position(
                tenant_id=tenant_id, account_id=acct.id, security_id=secs["BBB"].id, quantity=10,
                avg_cost=100.0, currency="USD", market_value_local=1000.0, as_of=TODAY - timedelta(days=1),
            ))
    session.commit()


def _get_accounts(client, tenant_id, pid, statements, *, cold: bool) -> list[str]:
    if cold:
        compute_cache.clear()
    statements.clear()
    r = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": str(tenant_id)})
    assert r.status_code == 200, r.text[:300]
    return list(statements)


def test_accounts_statement_count_does_not_grow_with_the_number_of_accounts(client, db_session, statements):
    """Same history, more accounts: the cold panel must send the SAME number of statements.
    Before, each extra ledger account cost ~24 and each snapshot account ~8."""
    secs = _seed_reference(db_session)
    tenant_a, pid_a = _seed(db_session, secs, months=12)
    tenant_b, pid_b = _seed(db_session, secs, months=12)
    _add_accounts(db_session, tenant_b, pid_b, secs, 4, "csv")
    _add_accounts(db_session, tenant_b, pid_b, secs, 4, "snaptrade")
    few = len(_get_accounts(client, tenant_a, pid_a, statements, cold=True))
    many = len(_get_accounts(client, tenant_b, pid_b, statements, cold=True))
    assert many == few, f"/accounts statements grew with account count: 3 accounts -> {few}, 11 accounts -> {many}"


def test_accounts_statement_ceiling(client, db_session, statements):
    """Absolute ceilings with headroom over the fixed code (36 cold; the code before the
    fix sent 79 on this seed). A new per-account read on this route trips them."""
    tenant_id, pid = _seed(db_session, _seed_reference(db_session), months=12)
    cold = len(_get_accounts(client, tenant_id, pid, statements, cold=True))
    warm = len(_get_accounts(client, tenant_id, pid, statements, cold=False))
    assert cold <= 42, f"cold /accounts sent {cold} statements"
    assert warm <= 25, f"warm /accounts sent {warm} statements"


def test_holdings_by_account_matches_the_per_account_read(db_session):
    """The batched read is a re-plumbing, not a re-definition: for every account, ledger-
    and snapshot-sourced alike, it returns exactly what ``holdings(account_id=...)`` does."""
    secs = _seed_reference(db_session)
    tenant_id, pid = _seed(db_session, secs, months=12)
    _add_accounts(db_session, tenant_id, pid, secs, 2, "csv")
    _add_accounts(db_session, tenant_id, pid, secs, 2, "snaptrade")
    ids = list(db_session.scalars(
        models.Account.__table__.select().with_only_columns(models.Account.id)
        .where(models.Account.portfolio_id == pid)
    ))
    compute_cache.clear()
    batched = analytics.holdings_by_account(db_session, tenant_id, pid, ids)
    assert set(batched) == set(ids)
    for aid in ids:
        assert batched[aid] == analytics.holdings(db_session, tenant_id, pid, account_id=aid), aid
    assert any(batched[a] for a in ids)
    assert isinstance(next(iter(batched)), uuid.UUID)
