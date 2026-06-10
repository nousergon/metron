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


def _latest(symbols, *, source=None):
    return {s: ClosePoint(date(2024, 2, 19), 100.0 + _off(s)) for s in symbols if s in _HELD}


def _sectors(symbols, *, source=None):
    return {s: _SECTORS[s] for s in symbols if s in _SECTORS}


def _bench(*, source=None):
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
            price_source=_full_hist, sector_source=_sectors, benchmark_source=lambda *, source=None: {},
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


class TestAttributionEndpoints:
    def test_compute_then_get(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        monkeypatch.setattr("api.services.prices.fetch_close_history", _full_hist)
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
