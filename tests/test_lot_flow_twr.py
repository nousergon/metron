"""Per-account TWR over the LOT-reconstruction path neutralizes contribution flow
(metron-ops#88).

Snapshot-sourced accounts (IBKR Flex / SnapTrade) have NO replayable trade feed — their
NAV history is rebuilt from the lot timeline, and a contribution shows up as a newly OPENED
lot. The flow the TWR neutralizes must therefore come from the lots (cost of opens, proceeds
of closes), NOT the Transaction ledger (which is empty for these accounts). The original bug:
flow was queried from BUY/SELL transactions → 0 at every step → the entire contribution-
driven NAV build-up read as investment return (live: per-account LTM of +135%…+366%).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.db import models
from api.services import performance
from api.services.persistence import persist_snapshot
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.schema import CanonicalAccount, CanonicalOpenLot, CanonicalSecurity


def _seed_price_bars(session, symbol: str, start: date, end: date, price_at) -> None:
    """Daily flat/curve PriceBars for ``symbol`` over [start, end]; ``price_at(d)`` → close."""
    sec = session.scalars(select(models.Security).where(models.Security.symbol == symbol)).first()
    assert sec is not None, f"security {symbol} not persisted"
    d = start
    while d <= end:
        session.add(models.PriceBar(security_id=sec.id, bar_date=d, close=price_at(d), currency="USD"))
        d += timedelta(days=1)
    session.commit()


def _seed_dca_lots(session, *, label: str, ticker: str = "AAPL") -> tuple[uuid.UUID, uuid.UUID]:
    """An ibkr_flex account funded by 12 monthly lots of ``ticker`` (12 sh @ $100 =
    $1,200 each). Pure contributions, no trade feed — the exact shape of the live
    snapshot accounts."""
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="P")
    session.add_all([tenant, portfolio])
    session.commit()
    lots = [
        CanonicalOpenLot(
            account_number="U1", security_id=f"EQ:{ticker}:USD", ticker=ticker,
            quantity=12, open_date=date(2025, m, 5), cost_basis=1200,
        )
        for m in range(1, 13)
    ]
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", label=label)],
        securities=[CanonicalSecurity(security_id=f"EQ:{ticker}:USD", ticker=ticker)],
        open_lots=lots,
    )
    persist_snapshot(session, tenant_id=tenant.id, portfolio_id=portfolio.id, snapshot=snapshot)
    return tenant.id, portfolio.id


def _cumulative_return(session, tenant_id, portfolio_id, today) -> float:
    series = performance.account_performance_series(
        session, tenant_id, portfolio_id, today=today, with_benchmarks=False
    )
    assert len(series.accounts) == 1
    pts = series.accounts[0].points
    assert len(pts) >= 2
    return pts[-1].g / pts[0].g - 1.0


def test_lot_contributions_are_not_read_as_return(db_session):
    tenant_id, portfolio_id = _seed_dca_lots(db_session, label="Dividend Anchor")
    _seed_price_bars(db_session, "AAPL", date(2025, 1, 1), date(2025, 12, 31), lambda _d: 100.0)
    # Flat prices, growth is 100% contributions → ~0% return (NOT hundreds of %, the bug).
    assert abs(_cumulative_return(db_session, tenant_id, portfolio_id, date(2025, 12, 31))) < 0.02


def test_lot_real_gain_is_captured_not_the_contributions(db_session):
    tenant_id, portfolio_id = _seed_dca_lots(db_session, label="Dividend Anchor")
    # Buys all at $100 (= market), then the whole book steps +10% near year-end.
    _seed_price_bars(
        db_session, "AAPL", date(2025, 1, 1), date(2025, 12, 31),
        lambda d: 110.0 if d >= date(2025, 12, 20) else 100.0,
    )
    # ~+10% (the appreciation), NOT the contribution build-up.
    assert _cumulative_return(db_session, tenant_id, portfolio_id, date(2025, 12, 31)) == pytest.approx(0.10, abs=0.02)


def test_transferred_lot_cost_basis_does_not_fabricate_return(db_session):
    """metron-ops#89: a lot whose recorded cost basis ≠ its market value at the recorded
    open_date — a transferred-in / re-imported position, or foreign-currency cost scale —
    must NOT fabricate a return. Valuing the lot's capital-in at MARKET (not cost) makes the
    opening step neutral by construction. Reproduces the live Roth IRA −78.8% single-day
    spike (a cluster of lots opened with cost ≈ 2.7× their market value)."""
    tenant = models.Tenant(id=uuid.uuid4(), name="t")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="P")
    db_session.add_all([tenant, portfolio])
    db_session.commit()
    snapshot = ConnectorSnapshot(
        source="ibkr_flex",
        accounts=[CanonicalAccount(number="U1", label="Roth")],
        securities=[CanonicalSecurity(security_id="EQ:AAPL:USD", ticker="AAPL")],
        open_lots=[
            # Baseline lot bought at market ($100) — establishes NAV history.
            CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL",
                             quantity=100, open_date=date(2025, 1, 6), cost_basis=10000),
            # Transferred-in lot: 100 sh, market value at open = 100 × $100 = $10k, but recorded
            # cost basis is $50k (bought high elsewhere / scale artifact). Cost ≠ market-at-open.
            CanonicalOpenLot(account_number="U1", security_id="EQ:AAPL:USD", ticker="AAPL",
                             quantity=100, open_date=date(2025, 6, 16), cost_basis=50000),
        ],
    )
    persist_snapshot(db_session, tenant_id=tenant.id, portfolio_id=portfolio.id, snapshot=snapshot)
    _seed_price_bars(db_session, "AAPL", date(2025, 1, 1), date(2025, 12, 31), lambda _d: 100.0)
    # Flat prices the whole way; the transfer adds capital, not return → ~0% (NOT −400%).
    assert abs(_cumulative_return(db_session, tenant.id, portfolio.id, date(2025, 12, 31))) < 0.02


def test_lot_flow_is_nonzero_across_opening_steps(db_session):
    """The mechanism guard: per-step flow must be NON-zero on sub-periods where lots open —
    the precise failure the original ``_scoped_net_purchases``-only flow produced (always 0)."""
    tenant_id, portfolio_id = _seed_dca_lots(db_session, label="Dividend Anchor")
    _seed_price_bars(db_session, "AAPL", date(2025, 1, 1), date(2025, 12, 31), lambda _d: 100.0)
    pts = performance._reconstruct_nav_points(
        db_session, tenant_id, portfolio_id, account_ids=None, today=date(2025, 12, 31), backfill=False
    )
    # 11 contributions land AFTER the first valuation point → ≥10 sub-periods carry +$1,200.
    contributing = [p for p in pts[1:] if p.flow > 0]
    assert len(contributing) >= 10
    assert all(p.flow == pytest.approx(1200.0, abs=1.0) for p in contributing)
    # Total neutralized flow ≈ the 11 post-base contributions (the first is the g0 base).
    assert sum(p.flow for p in pts[1:]) == pytest.approx(11 * 1200.0, rel=0.05)


def test_bounded_history_prefetch_equals_unbounded(db_session, monkeypatch):
    """metron-ops-I279: ``_account_performance_series``'s close-history prefetch (the
    ``nav_history`` fetch feeding every account's reconstruction) is now bounded to
    ``[min(lot open/close date, txn date), today]`` instead of reading the whole
    ``price_bars`` table — mirroring the SAME ``first`` bound
    ``_reconstruct_nav_points`` already uses for its own backfill.

    Two structurally-identical scenarios (distinct tickers, so both can share one
    DB/session without colliding on the securities' ``(symbol, currency)``
    uniqueness), each seeded with DECOY bars strictly before the earliest lot
    (2025-01-05) and strictly after ``today`` (2025-12-31), priced so they would
    visibly move the reconstructed growth series if a bound-computation bug let them
    leak in or dropped a real one. Scenario A runs through the real (bounded) fetch;
    scenario B monkeypatches it back to the pre-I279 unbounded call. The reconstructed
    growth series must come out byte-identical either way."""
    from api.services import prices as prices_module

    def _run(ticker):
        tenant_id, portfolio_id = _seed_dca_lots(db_session, label="Dividend Anchor", ticker=ticker)
        _seed_price_bars(db_session, ticker, date(2025, 1, 1), date(2025, 12, 31), lambda d: 100.0 + d.day * 0.01)
        sec = db_session.scalars(select(models.Security).where(models.Security.symbol == ticker)).first()
        db_session.add_all([
            models.PriceBar(security_id=sec.id, bar_date=date(2024, 12, 1), close=9999.0, currency="USD"),
            models.PriceBar(security_id=sec.id, bar_date=date(2026, 6, 1), close=0.01, currency="USD"),
        ])
        db_session.commit()
        series = performance.account_performance_series(
            db_session, tenant_id, portfolio_id, today=date(2025, 12, 31), with_benchmarks=False
        )
        assert len(series.accounts) == 1
        return [(p.when, round(p.g, 8)) for p in series.accounts[0].points]

    bounded = _run("AAPL")

    real = prices_module.close_history_by_symbol

    def _unbounded(session, symbols, *, start_date=None, end_date=None):
        return real(session, symbols)  # ignore the bound — the pre-I279 behaviour

    monkeypatch.setattr(prices_module, "close_history_by_symbol", _unbounded)
    unbounded = _run("MSFT")

    assert bounded == unbounded
