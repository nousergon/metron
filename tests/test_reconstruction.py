"""Historical price backfill + NAV reconstruction (C2-6b-2).

Forward-recording starts empty; reconstruction seeds the NAV series from past prices.
The price/history source is injected (never the network). Invariants: backfill is
idempotent; a position with no cached history on a date is excluded from that date's
NAV (never fabricated); reconstruction replays the ledger so positions are correct
as-of each valuation date; TWR/cumulative come out of the reconstructed series.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from api.db import models
from api.services import performance, prices
from portfolio_analytics.prices import ClosePoint, fetch_close_history

# AAPL bought Jan 15 (10 @100); MSFT bought Mar 10 (5 @200). No sells, no cash flows.
CSV = """date,type,symbol,quantity,price
2024-01-15,BUY,AAPL,10,100
2024-03-10,BUY,MSFT,5,200
"""

# Injected daily-close history (sparse — _asof carries forward to non-listed days).
_HIST = {
    "AAPL": [ClosePoint(date(2024, 1, 15), 100.0), ClosePoint(date(2024, 2, 1), 110.0), ClosePoint(date(2024, 3, 1), 120.0)],
    "MSFT": [ClosePoint(date(2024, 3, 10), 200.0), ClosePoint(date(2024, 3, 15), 210.0)],
    "SPY": [ClosePoint(date(2024, 1, 15), 480.0), ClosePoint(date(2024, 3, 1), 500.0)],
}


def _hist_src(symbols, start, end, *, source=None):
    return {s: _HIST[s] for s in symbols if s in _HIST}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    assert r.status_code == 200
    return pid


class TestHistorySource:
    def test_empty_and_bad_range(self):
        assert fetch_close_history([], date(2024, 1, 1), date(2024, 2, 1)) == {}
        assert fetch_close_history(["AAPL"], date(2024, 2, 1), date(2024, 1, 1), source=_hist_src) == {}

    def test_passthrough(self):
        out = fetch_close_history(["AAPL", "AAPL"], date(2024, 1, 1), date(2024, 4, 1), source=_hist_src)
        assert [p.close for p in out["AAPL"]] == [100.0, 110.0, 120.0]


class TestAsOfAndMonthEnds:
    def test_asof_carries_forward(self):
        s = _HIST["AAPL"]
        assert performance._asof_close(s, date(2024, 1, 14)) is None  # before first
        assert performance._asof_close(s, date(2024, 1, 20)) == 100.0  # carries Jan 15
        assert performance._asof_close(s, date(2024, 12, 31)) == 120.0  # carries Mar 1
        assert performance._asof_close(None, date(2024, 1, 1)) is None

    def test_month_ends(self):
        me = performance._month_ends(date(2024, 1, 15), date(2024, 3, 20))
        assert me == [date(2024, 1, 31), date(2024, 2, 29)]  # Mar 31 is past the end


class TestBackfill:
    def test_backfill_then_idempotent(self, client, db_session, tenant):
        pid = _seed(client, tenant)  # noqa: F841 — creates AAPL/MSFT securities
        n1 = prices.backfill_prices(db_session, ["AAPL", "MSFT"], date(2024, 1, 1), date(2024, 4, 1), source=_hist_src)
        assert n1 == 5  # 3 AAPL + 2 MSFT bars
        n2 = prices.backfill_prices(db_session, ["AAPL", "MSFT"], date(2024, 1, 1), date(2024, 4, 1), source=_hist_src)
        assert n2 == 0  # already cached and unchanged → no writes

    def test_backfill_updates_stale_close(self, client, db_session, tenant):
        """A spine refresh correcting a pre-split (or otherwise stale) close must overwrite."""
        _seed(client, tenant)  # creates AAPL security
        db_session.add(
            models.PriceBar(
                security_id=prices.ensure_security(db_session, "AAPL"),
                bar_date=date(2025, 7, 7),
                close=518.0,  # stale pre-split anchor
                currency="USD",
            )
        )
        db_session.commit()

        def corrected_src(symbols, start, end, *, source=None):
            return {
                "AAPL": [ClosePoint(date(2025, 7, 7), 126.36), ClosePoint(date(2026, 7, 2), 193.98)],
            }

        n = prices.backfill_prices(
            db_session, ["AAPL"], date(2025, 1, 1), date(2026, 7, 2), source=corrected_src
        )
        assert n == 2  # 1 update + 1 insert
        hist = prices.close_history_by_symbol(db_session, ["AAPL"])
        assert hist["AAPL"][0].close == pytest.approx(126.36)
        assert hist["AAPL"][-1].close == pytest.approx(193.98)

    def test_ensure_security_get_or_create(self, db_session):
        a = prices.ensure_security(db_session, "SPY")
        b = prices.ensure_security(db_session, "SPY")
        assert a == b  # idempotent

    def test_close_history_window_defaults_to_unbounded(self, client, db_session, tenant):
        """metron-ops-I279: ``start_date``/``end_date`` are additive. Calling with
        neither must reproduce the original unbounded read byte-for-byte."""
        _seed(client, tenant)  # creates AAPL security
        prices.backfill_prices(db_session, ["AAPL"], date(2024, 1, 1), date(2024, 4, 1), source=_hist_src)
        full = prices.close_history_by_symbol(db_session, ["AAPL"])
        assert full == prices.close_history_by_symbol(db_session, ["AAPL"], start_date=None, end_date=None)
        assert [p.close for p in full["AAPL"]] == [100.0, 110.0, 120.0]

    def test_close_history_window_filters_inclusive(self, client, db_session, tenant):
        """The window is inclusive on both ends and additive — it only ever narrows
        what the unbounded call already returns."""
        _seed(client, tenant)
        prices.backfill_prices(db_session, ["AAPL"], date(2024, 1, 1), date(2024, 4, 1), source=_hist_src)
        windowed = prices.close_history_by_symbol(
            db_session, ["AAPL"], start_date=date(2024, 1, 15), end_date=date(2024, 2, 1)
        )
        assert [p.bar_date for p in windowed["AAPL"]] == [date(2024, 1, 15), date(2024, 2, 1)]
        only_start = prices.close_history_by_symbol(db_session, ["AAPL"], start_date=date(2024, 2, 1))
        assert [p.bar_date for p in only_start["AAPL"]] == [date(2024, 2, 1), date(2024, 3, 1)]


class TestReconstruct:
    def test_reconstructs_nav_as_of_each_date(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        n = performance.reconstruct_snapshots(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 3, 20), source=_hist_src
        )
        # Valuation dates: Jan 15 (first), Jan 31, Feb 29 (month-ends), Mar 20 (today).
        assert n == 4

        p = client.get(f"/portfolios/{pid}/performance", headers={"X-Tenant-Id": tenant}).json()
        navs = {pt["snap_date"]: pt["nav"] for pt in p["points"]}
        assert navs["2024-01-15"] == 1000.0  # AAPL 10×100 (MSFT not yet held)
        assert navs["2024-02-29"] == 1100.0  # AAPL 10×110 (Feb 1 close carried)
        assert navs["2024-03-20"] == 2250.0  # AAPL 10×120 + MSFT 5×210
        assert p["latest_cost_basis"] == 2000.0
        assert p["points"][-1]["spy_close"] == 500.0
        # No flows → cumulative = TWR = 2250/1000 − 1 = 1.25.
        assert p["cumulative_return"] == pytest.approx(1.25)
        assert p["twr"] == pytest.approx(1.25)

    def test_reconstruct_idempotent(self, client, db_session, tenant):
        pid = _seed(client, tenant)
        performance.reconstruct_snapshots(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 3, 20), source=_hist_src)
        performance.reconstruct_snapshots(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 3, 20), source=_hist_src)
        from sqlalchemy import func, select

        from api.db import models

        n = db_session.scalar(
            select(func.count()).select_from(models.NavSnapshot).where(models.NavSnapshot.portfolio_id == uuid.UUID(pid))
        )
        assert n == 4  # re-run upserts, not duplicates

    def test_bounded_history_fetch_equals_unbounded(self, client, db_session, tenant, monkeypatch):
        """metron-ops-I279: ``_reconstruct_nav_points``'s internal close-history fetch
        (the ``if history is None`` fallback ``reconstruct_snapshots`` hits) is now
        bounded to the SAME ``first``..``today`` window it already computes for its own
        ``backfill_prices`` call a few lines up. Decoy bars dated well before the
        earliest BUY (2024-01-15) and after ``today`` — added once, since ``price_bars``
        is unscoped global reference data both portfolios read — prove the bound is a
        no-op: two structurally-identical portfolios (same CSV, different tenants)
        reconstruct through, respectively, the real bounded fetch and a monkeypatched
        pre-I279 unbounded one, and their resulting NAV series must match exactly."""
        pid_a = _seed(client, tenant)
        tenant2 = str(uuid.uuid4())
        pid_b = _seed(client, tenant2)

        aapl = db_session.scalars(select(models.Security).where(models.Security.symbol == "AAPL")).first()
        db_session.add_all([
            models.PriceBar(security_id=aapl.id, bar_date=date(2023, 1, 1), close=9999.0, currency="USD"),
            models.PriceBar(security_id=aapl.id, bar_date=date(2025, 1, 1), close=0.01, currency="USD"),
        ])
        db_session.commit()

        n_a = performance.reconstruct_snapshots(
            db_session, uuid.UUID(tenant), uuid.UUID(pid_a), today=date(2024, 3, 20), source=_hist_src
        )
        a_points = client.get(f"/portfolios/{pid_a}/performance", headers={"X-Tenant-Id": tenant}).json()["points"]

        real = prices.close_history_by_symbol

        def _unbounded(session, symbols, *, start_date=None, end_date=None):
            return real(session, symbols)  # ignore the bound — the pre-I279 behaviour

        monkeypatch.setattr(prices, "close_history_by_symbol", _unbounded)

        n_b = performance.reconstruct_snapshots(
            db_session, uuid.UUID(tenant2), uuid.UUID(pid_b), today=date(2024, 3, 20), source=_hist_src
        )
        b_points = client.get(f"/portfolios/{pid_b}/performance", headers={"X-Tenant-Id": tenant2}).json()["points"]

        assert n_a == n_b == 4
        assert a_points == b_points

    def test_endpoint_wires_reconstruction(self, client, tenant, monkeypatch):
        # Endpoint uses date.today(); just verify it wires through and populates history.
        monkeypatch.setattr("api.services.prices.fetch_close_history", _recent_hist_src)
        pid = _seed(client, tenant)
        p = client.post(f"/portfolios/{pid}/performance/reconstruct", headers={"X-Tenant-Id": tenant}).json()
        assert p["n_snapshots"] >= 2  # first + month-ends + today, all priced


def _recent_hist_src(symbols, start, end, *, source=None):
    # Flat $100 closes across the whole span so every valuation date is priceable.
    out = {}
    for s in symbols:
        out[s] = [ClosePoint(start, 100.0), ClosePoint(end, 100.0)]
    return out


class TestGuards:
    def test_backfill_empty_and_bad_range(self, db_session):
        assert prices.backfill_prices(db_session, [], date(2024, 1, 1), date(2024, 2, 1)) == 0
        assert prices.backfill_prices(db_session, ["AAPL"], date(2024, 2, 1), date(2024, 1, 1), source=_hist_src) == 0

    def test_backfill_skips_unknown_security(self, db_session):
        # Source prices a symbol with no securities row → nothing written.
        def src(symbols, start, end, *, source=None):
            return {"ZZZZ": [ClosePoint(date(2024, 1, 2), 9.0)]}

        assert prices.backfill_prices(db_session, ["ZZZZ"], date(2024, 1, 1), date(2024, 2, 1), source=src) == 0

    def test_close_history_empty(self, db_session):
        assert prices.close_history_by_symbol(db_session, []) == {}

    def test_reconstruct_no_transactions(self, client, db_session, tenant):
        pid = client.post("/portfolios", json={"name": "Empty"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        n = performance.reconstruct_snapshots(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 3, 20), source=_hist_src
        )
        assert n == 0
