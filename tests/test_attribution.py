"""Brinson-Fachler sector attribution (C2-6c-2).

Deterministic synthetic closes + injected sector/benchmark sources (never the
network). Invariants: a priced, classified portfolio decomposes its active return vs
SPY into allocation + selection + interaction that tie back to ``R_p − R_b``; an
unclassified holding lowers coverage (its MV isn't attributed to a guessed sector);
no priced holdings / no benchmark weights / no history each yield not-computable WITH
a reason.
"""

from __future__ import annotations

import io
import math
import uuid
from datetime import date, timedelta

import pytest

from api.config import settings
from api.services import attribution
from portfolio_analytics.prices import ClosePoint

# Two holdings in two different GICS sectors → allocation + selection both exercised.
CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n2024-01-01,BUY,XOM,5,100\n"

_HELD = {"AAPL", "XOM"}
_SECTORS = {"AAPL": "Technology", "XOM": "Energy"}
# A benchmark with the two held sectors plus others (renormalized internally to 1).
_BENCH = {"Technology": 0.30, "Energy": 0.04, "Healthcare": 0.13, "Industrials": 0.09}


def _off(sym: str) -> int:
    return sum(ord(c) for c in sym) % 7


def _closes(sym: str, n: int = 50, start: date = date(2024, 1, 1)) -> list[ClosePoint]:
    base = 100.0 + _off(sym)
    return [
        ClosePoint(start + timedelta(days=i), round(base * (1 + 0.01 * math.sin(i + _off(sym) * 0.3)), 4))
        for i in range(n)
    ]


def _full_hist(symbols, start, end, *, source=None):
    return {s: _closes(s) for s in symbols}


def _recent_hist(symbols, start, end, *, source=None):
    """``_full_hist`` shifted to end yesterday, for routes that compute as of today."""
    return {s: _closes(s, start=date.today() - timedelta(days=50)) for s in symbols}


def _latest(symbols, *, source=None):
    return {s: ClosePoint(date(2024, 2, 19), 100.0 + _off(s)) for s in symbols if s in _HELD}


def _sectors(symbols, *, source=None):
    return {s: _SECTORS[s] for s in symbols if s in _SECTORS}


def _bench(symbol="SPY", *, source=None):
    return dict(_BENCH)


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant, csv=CSV):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    assert client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    ).status_code == 200
    return pid


def _refresh(client, tenant, pid, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _latest)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
    client.post(f"/portfolios/{pid}/prices/refresh", headers={"X-Tenant-Id": tenant})


class TestComputeAttribution:
    def test_full_decomposition_ties_out(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        a = attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid),
            today=date(2024, 2, 20), do_backfill=True,
            price_source=_full_hist, sector_source=_sectors, benchmark_source=_bench,
        )
        assert a.computable is True
        # as_of is the freshest close bar the window returns used (last synthetic bar =
        # 2024-02-19), never the compute-call date (2024-02-20).
        assert a.as_of == date(2024, 2, 19)
        assert a.coverage == pytest.approx(1.0)  # both holdings classified
        held_sectors = {e.sector for e in a.sectors if e.port_weight > 0}
        assert held_sectors == {"Technology", "Energy"}
        # Brinson-Fachler ties out: allocation + selection + interaction == active return.
        assert a.active_return == pytest.approx(a.allocation + a.selection + a.interaction)
        assert a.active_return == pytest.approx(a.portfolio_return - a.benchmark_return)
        # Per-sector effects sum to the totals.
        assert sum(e.allocation for e in a.sectors) == pytest.approx(a.allocation)
        assert all(e.total == pytest.approx(e.allocation + e.selection + e.interaction) for e in a.sectors)

    def test_no_priced_holdings(self, client, db_session, tenant):
        pid = _seed(client, tenant)  # never refreshed → no market value
        a = attribution.compute_attribution(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 2, 20))
        assert a.computable is False and "priced" in a.reason.lower()

    def test_benchmark_weights_unavailable(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        a = attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid),
            today=date(2024, 2, 20), do_backfill=True,
            price_source=_full_hist, sector_source=_sectors, benchmark_source=lambda symbol="SPY", *, source=None: {},
        )
        assert a.computable is False and "benchmark" in a.reason.lower()

    def test_unclassified_holding_lowers_coverage(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        def only_aapl(symbols, *, source=None):  # XOM left unclassified
            return {"AAPL": "Technology"}

        a = attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid),
            today=date(2024, 2, 20), do_backfill=True,
            price_source=_full_hist, sector_source=only_aapl, benchmark_source=_bench,
        )
        assert a.computable is True
        assert 0.0 < a.coverage < 1.0  # XOM's MV is uncovered, not attributed to a guess
        assert {e.sector for e in a.sectors if e.port_weight > 0} == {"Technology"}

    def test_insufficient_history_without_backfill(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        # No cached ETF history and do_backfill=False → benchmark returns can't be built.
        a = attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid),
            today=date(2024, 2, 20), do_backfill=False, benchmark_source=_bench,
        )
        assert a.computable is False and a.reason


class TestWindowedRead:
    """metron-ops-I343: attribution reads only closes on/after its window start.

    ``_window_return`` anchors on the first close on/after ``start``, so bars before it
    never changed a result; reading them was pure egress."""

    TODAY = date(2024, 2, 20)

    def test_bars_before_the_window_leave_the_result_unchanged(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        kw = dict(today=self.TODAY, sector_source=_sectors, benchmark_source=_bench)
        before = attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), do_backfill=True, price_source=_full_hist, **kw
        )
        from api.services import prices

        def old(symbols, start, end, *, source=None):
            return {s: [ClosePoint(date(2023, 6, 1) + timedelta(days=i), 10.0 + i) for i in range(60)] for s in symbols}

        prices.backfill_prices(db_session, [*_HELD, *attribution.SECTOR_ETF.values()], date(2023, 6, 1), self.TODAY, source=old)
        after = attribution.compute_attribution(db_session, uuid.UUID(tenant), uuid.UUID(pid), **kw)
        assert before.computable and after.computable
        assert after.active_return == pytest.approx(before.active_return)
        assert after.portfolio_return == pytest.approx(before.portfolio_return)
        assert after.as_of == before.as_of

    def test_history_read_is_bounded_at_the_window_start(self, client, db_session, tenant, monkeypatch):
        from api.services import prices

        seen = []
        real = prices.close_history_by_symbol
        monkeypatch.setattr(
            prices, "close_history_by_symbol",
            lambda session, symbols, **kw: seen.append(kw) or real(session, symbols, **kw),
        )
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        attribution.compute_attribution(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=self.TODAY, benchmark_source=_bench
        )
        assert seen and seen[-1].get("start_date") == self.TODAY - timedelta(days=90)


class TestAttributionEndpoints:
    def test_compute_then_get(self, client, tenant, monkeypatch):
        # Attribution is feed-dependent; the endpoint enforces the entitlement matrix,
        # so this models a feed-entitled deployment (feed_entitled — decoupled from the
        # S3 market_data_sync_enabled infra toggle per metron-ops#43; see test_risk's
        # test_compute_then_get + test_entitlements_enforcement.py).
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        # The routes compute as of date.today() over a trailing window, so the cached
        # history has to end near today, not in 2024 (metron-ops-I343).
        monkeypatch.setattr("api.services.prices.fetch_close_history", _recent_hist)
        monkeypatch.setattr("api.services.sectors.fetch_sectors", _sectors)
        monkeypatch.setattr("api.services.attribution.fetch_benchmark_sector_weights", _bench)
        posted = client.post(f"/portfolios/{pid}/attribution/compute", headers={"X-Tenant-Id": tenant}).json()
        assert posted["computable"] is True
        assert posted["active_return"] == pytest.approx(posted["allocation"] + posted["selection"] + posted["interaction"])
        # GET now computes from the cache + sectors the POST populated.
        got = client.get(f"/portfolios/{pid}/attribution", headers={"X-Tenant-Id": tenant}).json()
        assert got["computable"] is True

    def test_attribution_requires_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.get(
            f"/portfolios/{pid}/attribution", headers={"X-Tenant-Id": str(uuid.uuid4())}
        ).status_code == 404
