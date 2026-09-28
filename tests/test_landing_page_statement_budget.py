"""Statement budget for the Holdings landing page (``/portfolios/{id}``).

The page is a fan-out of ~11 API requests that all run on ONE gunicorn worker against a
Neon database a network round trip away, so what it costs is dominated by how many SQL
statements each request sends, not by how many rows come back. A production-shaped
benchmark (66 holdings, 5 accounts, three foreign currencies, three years of history)
measured 1,276 statements for a first load of the morning and 355 for a warm one, which
at the ~18 ms per statement the nightly ``db_read_profile`` artifact records on the box is
the 5-10 s the owner saw on 2026-09-28. Three read patterns carried almost all of it:

* ``compute_cache.portfolio_fingerprint`` — 8 aggregate queries, recomputed by every
  cached read, several times per request (232 of the 355 warm statements);
* a per-(currency, date) FX lookup inside NAV reconstruction and the income / realized /
  transactions conversions (568 statements in one cold Accounts-panel request);
* a per-(sub-period, account) net-purchases query inside NAV reconstruction.

The last two scale with the length of the portfolio's history, so they got slower every
month without any code changing. These tests pin the fixed shape: one fingerprint per
request, and a cold-cache statement count that does NOT grow with history.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import event

from api.db import models
from api.services import compute_cache

TODAY = date.today()

# Every read the landing page makes on first load (web/app/portfolios/[id]/page.tsx, its
# streamed sections, and the client IntradayRefresher's first poll).
LANDING_PAGE = (
    "/holdings-view",
    "/intraday",
    "/summary?valuation=live",
    "/today",
    "/intraday-legs",
    "/holdings?valuation=live",
    "/valuation-medians",
    "/accounts",
    "/watchlist",
)


CCY = {"AAA": "USD", "BBB": "USD", "RMS": "EUR", "NOVN": "CHF", "CCC": "USD", "SU": "EUR", "SPY": "USD"}
HISTORY_MONTHS = 24


def _seed_reference(session) -> dict[str, models.Security]:
    """The GLOBAL reference data (securities, weekly closes, weekly EUR/CHF rates) — shared
    by every portfolio a test seeds, and always the full ``HISTORY_MONTHS``."""
    secs = {t: models.Security(symbol=t, name=t, currency=c, yf_symbol=t, asset_class="equity") for t, c in CCY.items()}
    session.add_all(secs.values())
    session.flush()
    start = TODAY - timedelta(days=30 * HISTORY_MONTHS)
    weeks = [start + timedelta(days=7 * i) for i in range((TODAY - start).days // 7 + 1)]
    for sec in secs.values():
        session.add_all(
            models.PriceBar(security_id=sec.id, bar_date=d, close=100.0 + i * 0.1, currency=sec.currency)
            for i, d in enumerate(weeks)
        )
    for c, r in (("EUR", 1.1), ("CHF", 1.2)):
        session.add_all(models.FxRate(currency=c, base="USD", rate_date=d, rate=r) for d in weeks)
    session.commit()
    return secs


def _seed(session, secs: dict[str, models.Security], *, months: int) -> tuple[uuid.UUID, uuid.UUID]:
    """A multi-account, multi-currency portfolio with ``months`` of history: an IBKR-style
    snapshot account (positions + dated open lots + broker realized lots, two of its
    tickers in EUR/CHF), a SnapTrade-style snapshot account, and a CSV ledger account
    with monthly buys and quarterly foreign dividends."""
    ccy = CCY
    tenant = models.Tenant(name="budget")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    ibkr = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="ibkr_flex", external_id="U1",
                          name="IBKR", currency="USD", cash_balance_usd=1000.0)
    snap = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade", external_id="S1",
                          name="Snap", currency="USD", cash_balance_usd=500.0)
    ledger = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="C1",
                            name="CSV", currency="USD")
    session.add_all([ibkr, snap, ledger])
    session.flush()
    start = TODAY - timedelta(days=30 * months)

    # Snapshot accounts: current positions + one open lot per ticker per quarter + closed lots.
    for acct, tickers in ((ibkr, ("AAA", "RMS", "NOVN")), (snap, ("BBB",))):
        for t in tickers:
            session.add(models.Position(tenant_id=tenant.id, account_id=acct.id, security_id=secs[t].id,
                                        quantity=10 * months, avg_cost=100.0, currency=ccy[t],
                                        market_value_local=1000.0 * months, as_of=TODAY - timedelta(days=1)))
            if acct is ibkr:
                for q in range(0, months, 3):
                    session.add(models.OpenLot(tenant_id=tenant.id, account_id=acct.id, ticker=t, quantity=30,
                                               open_date=start + timedelta(days=30 * q), cost_basis=3000.0,
                                               currency=ccy[t], source="ibkr_flex"))
                    session.add(models.RealizedLot(tenant_id=tenant.id, account_id=acct.id, ticker=t,
                                                   open_date=start + timedelta(days=30 * q),
                                                   close_date=start + timedelta(days=30 * q + 45), quantity=5,
                                                   proceeds=600.0, cost_basis=500.0, currency=ccy[t],
                                                   source="ibkr_flex", lot_key=f"{t}-{q}"))
    # Ledger account: monthly buys, quarterly EUR dividends, one deposit.
    session.add(models.Transaction(tenant_id=tenant.id, account_id=ledger.id, security_id=None, txn_type="DEPOSIT",
                                   quantity=0, price=0, amount=100000.0, currency="USD", trade_date=start,
                                   source_key="dep"))
    for m in range(months):
        d = start + timedelta(days=30 * m + 1)
        for t in ("CCC", "SU"):
            session.add(models.Transaction(tenant_id=tenant.id, account_id=ledger.id, security_id=secs[t].id,
                                           txn_type="BUY", quantity=2, price=100.0, amount=200.0, currency=ccy[t],
                                           trade_date=d, source_key=f"b-{t}-{m}"))
        if m % 3 == 2:
            session.add(models.Transaction(tenant_id=tenant.id, account_id=ledger.id, security_id=secs["SU"].id,
                                           txn_type="DIVIDEND", quantity=0, price=0, amount=15.0, currency="EUR",
                                           trade_date=d, source_key=f"d-{m}"))
    session.add(models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=pf.id, intraday_enabled=True))
    session.add(models.WatchlistItem(tenant_id=tenant.id, portfolio_id=pf.id, symbol="AAA"))
    session.commit()
    return tenant.id, pf.id


@pytest.fixture()
def statements(_engine):
    """Every SQL statement the engine executes, in order."""
    seen: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        seen.append(statement)

    event.listen(_engine, "after_cursor_execute", _count)
    yield seen
    event.remove(_engine, "after_cursor_execute", _count)


def _load_page(client, tenant_id, pid, statements) -> dict[str, list[str]]:
    per_request: dict[str, list[str]] = {}
    for path in LANDING_PAGE:
        statements.clear()
        r = client.get(f"/portfolios/{pid}{path}", headers={"X-Tenant-Id": str(tenant_id)})
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        per_request[path] = list(statements)
    return per_request


def _fingerprints(stmts: list[str]) -> int:
    # The fingerprint's transactions term is the one statement fragment nothing else sends.
    return sum("count(transactions.id)" in s for s in stmts)


def test_each_request_computes_the_portfolio_fingerprint_at_most_once(client, db_session, statements):
    tenant_id, pid = _seed(db_session, _seed_reference(db_session), months=12)
    for warmth in ("cold", "warm"):
        if warmth == "cold":
            compute_cache.clear()
        per_request = _load_page(client, tenant_id, pid, statements)
        over = {p: _fingerprints(s) for p, s in per_request.items() if _fingerprints(s) > 1}
        assert not over, f"{warmth} page recomputed the fingerprint more than once per request: {over}"


def test_first_load_statement_count_does_not_grow_with_history(client, db_session, statements):
    """Double the history and the cold page must send the SAME number of statements. On
    the per-date FX / per-sub-period flow reads it grew by hundreds (one round trip per
    valuation date per currency, per account)."""
    secs = _seed_reference(db_session)
    tenant_a, pid_a = _seed(db_session, secs, months=12)
    tenant_b, pid_b = _seed(db_session, secs, months=24)
    compute_cache.clear()
    short = _load_page(client, tenant_a, pid_a, statements)
    compute_cache.clear()
    long = _load_page(client, tenant_b, pid_b, statements)
    grew = {p: (len(short[p]), len(long[p])) for p in LANDING_PAGE if len(long[p]) != len(short[p])}
    assert not grew, f"statements per request grew with history length (12mo, 24mo): {grew}"


def test_landing_page_statement_ceiling(client, db_session, statements):
    """Absolute ceilings, with headroom over the fixed code (165 cold / 86 warm on this
    seed; the code before the fix sent 407 / 246). A new per-row or per-date read on this
    page trips them."""
    tenant_id, pid = _seed(db_session, _seed_reference(db_session), months=12)
    compute_cache.clear()
    cold = sum(len(s) for s in _load_page(client, tenant_id, pid, statements).values())
    warm = sum(len(s) for s in _load_page(client, tenant_id, pid, statements).values())
    assert cold <= 190, f"cold landing page sent {cold} statements"
    assert warm <= 100, f"warm landing page sent {warm} statements"
