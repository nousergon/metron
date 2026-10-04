"""NAV reconstruction reads each symbol's closes only over the dates it can be valued at
(metron-ops-I343).

``_reconstruct_nav_points`` used to read every close for every symbol ever held from the
portfolio's first lot or transaction to today (``[first, today]``). That one read was the
largest left in the nightly refresh profile, three refreshes a night. It now reads each
symbol over its own window (``performance._close_read_windows``) plus one carry-forward
bar (``prices.close_history_in_windows``).

The test that matters is equality. On every golden portfolio below, the NAV series has to
come out identical, point for point and float for float, whether it is built from the
windowed read or from the old ``[first, today]`` read. "Before" is the old read put back
with ``monkeypatch``, in the same database and session, so nothing else differs.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from api.db import models
from api.services import db_read_profile, demo, demo_household, performance, prices
from portfolio_analytics.domain.ledger import Transaction, TxnType
from portfolio_analytics.prices import ClosePoint

PROFILE_LABEL = "performance.close_history_by_symbol"


def _restore_first_today_read(monkeypatch) -> None:
    """Put back the read this change replaced: every window's symbol over
    ``[floor, today]``. ``today`` is the largest ``hi``, since SPY's window always ends
    there."""
    real = prices.close_history_by_symbol

    def _first_today(session, windows, *, floor):
        return real(session, list(windows), start_date=floor, end_date=max(hi for _lo, hi in windows.values()))

    monkeypatch.setattr(prices, "close_history_in_windows", _first_today)


def _series(session, tenant_id, portfolio_id, today, account_ids=None):
    return performance._reconstruct_nav_points(
        session, tenant_id, portfolio_id, account_ids=account_ids, today=today, backfill=False
    )


def _account_ids(session, portfolio_id):
    return sorted(
        session.scalars(select(models.Account.id).where(models.Account.portfolio_id == portfolio_id)).all(),
        key=str,
    )


def _windowed_vs_first_today(session, monkeypatch, tenant_id, portfolio_id, today):
    """Every series reconstruction can produce for the portfolio (whole book, then each
    account), built twice. Returns ``(after, before)``."""
    scopes = [None, *([aid] for aid in _account_ids(session, portfolio_id))]
    after = [_series(session, tenant_id, portfolio_id, today, scope) for scope in scopes]
    with monkeypatch.context() as m:
        _restore_first_today_read(m)
        before = [_series(session, tenant_id, portfolio_id, today, scope) for scope in scopes]
    return after, before


# ── A lot-and-ledger book built to hit every edge of the window ─────────────────────

TODAY = date(2025, 3, 31)
BAR_START = date(2021, 6, 1)  # well before the first lot or transaction: decoys
BAR_END = date(2025, 6, 30)  # well after TODAY: decoys


def _price(symbol: str, d: date) -> float:
    """Distinct per symbol and per day, so a wrong bar cannot hide behind an equal one."""
    base = 50.0 + 13.0 * (ord(symbol[0]) - ord("A"))
    return round(base + (d - BAR_START).days * 0.07 + (d.day % 5) * 0.31, 4)


def _seed_bars(session, sec: models.Security, start: date, end: date, *, skip=()) -> None:
    d = start
    while d <= end:
        if d.weekday() < 5 and not any(lo <= d <= hi for lo, hi in skip):
            session.add(models.PriceBar(security_id=sec.id, bar_date=d, close=_price(sec.symbol, d), currency="USD"))
        d += timedelta(days=1)


def _seed_edge_book(session) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = models.Tenant(id=uuid.uuid4(), name="edge")
    portfolio = models.Portfolio(id=uuid.uuid4(), tenant_id=tenant.id, name="Edge book")
    session.add_all([tenant, portfolio])
    session.flush()
    secs = {s: models.Security(symbol=s, currency="USD") for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "SPY")}
    session.add_all(secs.values())
    session.flush()

    # Daily weekday bars from before the book starts to after TODAY, for every symbol.
    # CCC has a four-week hole around its lot's open date, so the carry-forward bar sits
    # well before the window opens. GGG has no bars until a year after its lot opened,
    # so its window has no carry-forward bar at all.
    for s, sec in secs.items():
        if s == "CCC":
            _seed_bars(session, sec, BAR_START, BAR_END, skip=[(date(2024, 11, 1), date(2024, 11, 29))])
        elif s == "GGG":
            _seed_bars(session, sec, date(2023, 1, 2), BAR_END)
        else:
            _seed_bars(session, sec, BAR_START, BAR_END)

    lots = models.Account(tenant_id=tenant.id, portfolio_id=portfolio.id, broker="ibkr_flex", external_id="U-EDGE")
    ledger = models.Account(tenant_id=tenant.id, portfolio_id=portfolio.id, broker="csv", external_id="CSV-EDGE")
    session.add_all([lots, ledger])
    session.flush()

    def _open(ticker, opened, qty, cb):
        session.add(models.OpenLot(
            tenant_id=tenant.id, account_id=lots.id, ticker=ticker, quantity=qty, open_date=opened,
            cost_basis=cb, source="ibkr_flex",
        ))

    def _closed(ticker, opened, closed, qty, cb, proceeds):
        session.add(models.RealizedLot(
            tenant_id=tenant.id, account_id=lots.id, ticker=ticker, open_date=opened, close_date=closed,
            quantity=qty, cost_basis=cb, proceeds=proceeds, source="ibkr_flex",
            lot_key=f"{ticker}-{opened}-{closed}",
        ))

    _open("AAA", date(2022, 3, 7), 10, 1000)
    _open("AAA", date(2023, 6, 10), 5, 600)  # a Saturday: valued at Friday's close
    _closed("BBB", date(2022, 1, 3), date(2022, 9, 15), 20, 1400, 1900)  # exited...
    _closed("BBB", date(2024, 2, 1), date(2024, 5, 1), 8, 900, 1000)  # ...and bought back
    _open("CCC", date(2024, 11, 23), 7, 700)  # inside CCC's hole in the bars
    _closed("DDD", date(2023, 2, 10), date(2023, 1, 10), 3, 300, 310)  # closes before it opens
    _closed("EEE", date(2023, 4, 3), date(2023, 4, 3), 4, 400, 410)  # same-day round trip
    _open("GGG", date(2022, 1, 3), 6, 600)  # held a year before any GGG bar exists

    def _txn(kind, when, sec=None, qty=0.0, price=0.0, amount=0.0):
        session.add(models.Transaction(
            tenant_id=tenant.id, account_id=ledger.id, security_id=sec.id if sec else None,
            txn_type=kind.value, quantity=qty, price=price, amount=amount, currency="USD",
            trade_date=when, source_key=f"{kind.value}-{when}-{sec.symbol if sec else ''}",
        ))

    _txn(TxnType.DEPOSIT, date(2021, 12, 1), amount=10_000)  # `first`: before any ticker
    _txn(TxnType.BUY, date(2022, 5, 2), secs["FFF"], qty=30, price=60, amount=1800)
    _txn(TxnType.SELL, date(2023, 3, 1), secs["FFF"], qty=30, price=80, amount=2400)  # fully sold
    _txn(TxnType.BUY, date(2023, 8, 1), secs["AAA"], qty=2, price=150, amount=300)  # shares a ticker with the lots
    session.commit()
    return tenant.id, portfolio.id


class TestEdgeBook:
    def test_nav_series_identical_to_the_first_today_read(self, db_session, monkeypatch):
        tenant_id, portfolio_id = _seed_edge_book(db_session)
        after, before = _windowed_vs_first_today(db_session, monkeypatch, tenant_id, portfolio_id, TODAY)
        assert len(after) == 3  # the whole book, then each of the two accounts
        assert all(len(series) > 20 for series in after)
        assert after == before

    def test_persisted_snapshots_identical_and_the_profiled_read_shrinks(self, db_session, monkeypatch):
        """The write path the refresh runs (``reconstruct_snapshots``, with the run
        profile), compared on what it persists and on the rows the profiled block reads."""
        tenant_id, portfolio_id = _seed_edge_book(db_session)

        def _run():
            profile = db_read_profile.ReadProfile("daily-refresh")
            written = performance.reconstruct_snapshots(
                db_session, tenant_id, portfolio_id, today=TODAY, source=lambda *a, **k: {}, profile=profile
            )
            rows = db_session.execute(
                select(
                    models.NavSnapshot.snap_date, models.NavSnapshot.nav, models.NavSnapshot.cost_basis,
                    models.NavSnapshot.external_flow, models.NavSnapshot.spy_close,
                )
                .where(models.NavSnapshot.portfolio_id == portfolio_id)
                .order_by(models.NavSnapshot.snap_date)
            ).all()
            return written, rows, profile.blocks[PROFILE_LABEL].rows

        written_after, persisted_after, rows_after = _run()
        with monkeypatch.context() as m:
            _restore_first_today_read(m)
            written_before, persisted_before, rows_before = _run()

        assert written_after == written_before > 20
        assert persisted_after == persisted_before
        assert rows_after < rows_before

    def test_windows_are_the_dates_reconstruction_values_at(self, db_session):
        tenant_id, portfolio_id = _seed_edge_book(db_session)
        open_lots, closed_lots = performance._load_lot_timeline(db_session, tenant_id, portfolio_id)
        ledger = [t for _aid, t in performance.analytics.engine_transactions_by_account(
            db_session, tenant_id, portfolio_id
        )]
        first = date(2021, 12, 1)
        windows = performance._close_read_windows(open_lots, closed_lots, ledger, first, TODAY)
        assert windows == {
            "AAA": (date(2022, 3, 7), TODAY),  # lots and a ledger buy: the hull
            "BBB": (date(2022, 1, 3), date(2024, 5, 1)),  # both round trips, gap included
            "CCC": (date(2024, 11, 23), TODAY),
            "DDD": (date(2023, 1, 10), date(2023, 2, 10)),  # reversed dates, both events covered
            "EEE": (date(2023, 4, 3), date(2023, 4, 3)),
            "FFF": (date(2022, 5, 2), TODAY),  # a ledger ticker stays open-ended
            "GGG": (date(2022, 1, 3), TODAY),
            "SPY": (first, TODAY),
        }


class TestWindowedRead:
    """``close_history_in_windows`` against the plain reader it narrows."""

    @pytest.fixture()
    def secs(self, db_session):
        _seed_edge_book(db_session)
        return {s.symbol: s for s in db_session.scalars(select(models.Security)).all()}

    def test_window_plus_one_carry_forward_bar(self, db_session, secs):
        got = prices.close_history_in_windows(
            db_session, {"AAA": (date(2023, 6, 10), date(2023, 6, 16))}, floor=date(2021, 12, 1)
        )
        assert [p.bar_date for p in got["AAA"]] == [date(2023, 6, d) for d in (9, 12, 13, 14, 15, 16)]

    def test_carry_forward_reaches_back_over_a_hole(self, db_session, secs):
        got = prices.close_history_in_windows(
            db_session, {"CCC": (date(2024, 11, 23), date(2024, 12, 3))}, floor=date(2021, 12, 1)
        )
        assert [p.bar_date for p in got["CCC"]] == [date(2024, 10, 31), date(2024, 12, 2), date(2024, 12, 3)]

    def test_carry_forward_never_reaches_below_the_floor(self, db_session, secs):
        got = prices.close_history_in_windows(
            db_session, {"GGG": (date(2022, 1, 3), date(2023, 1, 4))}, floor=date(2021, 12, 1)
        )
        assert [p.bar_date for p in got["GGG"]] == [date(2023, 1, 2), date(2023, 1, 3), date(2023, 1, 4)]
        floored = prices.close_history_in_windows(
            db_session, {"AAA": (date(2023, 6, 10), date(2023, 6, 12))}, floor=date(2023, 6, 10)
        )
        assert [p.bar_date for p in floored["AAA"]] == [date(2023, 6, 12)]

    def test_every_asof_inside_the_window_matches_the_wide_read(self, db_session, secs):
        floor = date(2021, 12, 1)
        windows = {
            "AAA": (date(2023, 6, 10), date(2023, 9, 30)),
            "BBB": (date(2022, 1, 3), date(2022, 9, 15)),
            "CCC": (date(2024, 11, 23), TODAY),
            "GGG": (date(2022, 1, 3), date(2023, 3, 1)),
        }
        narrow = prices.close_history_in_windows(db_session, windows, floor=floor)
        wide = prices.close_history_by_symbol(db_session, list(windows), start_date=floor, end_date=TODAY)
        for symbol, (lo, hi) in windows.items():
            assert len(narrow.get(symbol, [])) < len(wide[symbol])
            d = lo
            while d <= hi:
                assert performance._asof_close(narrow.get(symbol), d) == performance._asof_close(wide[symbol], d)
                d += timedelta(days=1)

    def test_empty_and_inverted_windows_read_nothing(self, db_session, secs):
        assert prices.close_history_in_windows(db_session, {}, floor=date(2021, 12, 1)) == {}
        assert prices.close_history_in_windows(
            db_session, {"AAA": (date(2024, 1, 2), date(2024, 1, 1)), "": (date(2024, 1, 1), TODAY)},
            floor=date(2021, 12, 1),
        ) == {}

    def test_returns_close_points_ascending(self, db_session, secs):
        got = prices.close_history_in_windows(
            db_session, {"SPY": (date(2025, 3, 26), date(2025, 3, 28))}, floor=date(2025, 3, 26)
        )
        assert got == {"SPY": [ClosePoint(date(2025, 3, d), _price("SPY", date(2025, 3, d))) for d in (26, 27, 28)]}


# ── The committed golden portfolios ────────────────────────────────────────────────


def test_demo_household_nav_series_identical(db_session, monkeypatch):
    """The ICP-shaped demo household (metron-ops-I317): three ledger accounts, ~25
    holdings bought over five years, two realized sales. Its TWR/MWR goldens are pinned
    in ``tests/test_demo_household.py``."""
    demo_household.ensure_demo_household_seeded(db_session)
    after, before = _windowed_vs_first_today(
        db_session, monkeypatch, demo_household.DEMO_TENANT_ID, demo_household.DEMO_HOUSEHOLD_PORTFOLIO_ID,
        date(2026, 8, 15),
    )
    assert len(after) == 4
    assert all(len(series) >= 2 for series in after)
    assert after == before


def test_showcase_portfolio_nav_series_identical(db_session, monkeypatch):
    """The Showcase Portfolio's frozen sample sleeve (two accounts, non-equity classes,
    dividends), replayed through the same CSV bridge a real upload uses."""
    demo.ensure_reference_seeded(db_session)
    after, before = _windowed_vs_first_today(
        db_session, monkeypatch, demo.DEMO_TENANT_ID, demo.REFERENCE_PORTFOLIO_ID, TODAY
    )
    assert any(len(series) >= 2 for series in after)
    assert after == before


def test_ledger_transactions_used_for_windows_are_the_reconstruction_ledger():
    """``_close_read_windows`` takes ``Transaction`` rows exactly as reconstruction
    replays them, and ignores the empty ticker of a cash row."""
    windows = performance._close_read_windows(
        [], [], [Transaction(date(2024, 1, 2), TxnType.DEPOSIT, amount=5.0)], date(2024, 1, 2), date(2024, 2, 1)
    )
    assert windows == {"SPY": (date(2024, 1, 2), date(2024, 2, 1))}
