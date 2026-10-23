"""Sector attribution measures every leg over one dated window (metron-ops-I372).

Reproduced against ``main`` before this file existed:

* ``_window_return`` anchored each series independently on its own first close, so a
  holding with a short (or stale) history entered the same Brinson sum over a different
  interval than every other leg — while the summary still reported the requested window;
* a classified holding with NO window return still counted in the sector weights and in
  ``coverage``, though no return of it was attributed;
* ``as_of`` was the FRESHEST close across all series, so one fresh series dated an
  otherwise stale decomposition.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from api.config import settings
from api.services import attribution
from tests.test_attribution import _bench, _closes, _refresh, _sectors, _seed

TODAY = date(2024, 2, 20)
FIRST = date(2024, 1, 1)
LAST = date(2024, 2, 19)  # `_closes` default: 50 daily bars from FIRST


def _source(overrides: dict[str, list]):
    def source(symbols, start, end, *, source=None):
        return {s: overrides.get(s, _closes(s)) for s in symbols}
    return source


def _compute(client, db_session, tenant, monkeypatch, overrides):
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    return attribution.compute_attribution(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), today=TODAY, do_backfill=True,
        price_source=_source(overrides), sector_source=_sectors, benchmark_source=_bench,
    )


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _ret(series, anchor, end):
    first = [p for p in series if p.bar_date <= anchor][-1].close
    last = [p for p in series if p.bar_date <= end][-1].close
    return last / first - 1.0


def test_every_leg_is_measured_over_the_reported_window(client, db_session, tenant, monkeypatch):
    a = _compute(client, db_session, tenant, monkeypatch, {})
    assert a.computable
    assert (a.start_date, a.as_of) == (FIRST, LAST)
    tech = next(e for e in a.sectors if e.sector == "Technology")
    assert tech.port_return == pytest.approx(_ret(_closes("AAPL"), FIRST, LAST))
    assert tech.bench_return == pytest.approx(_ret(_closes("XLK"), FIRST, LAST))
    assert a.unattributed == []
    assert a.requested_start == TODAY - timedelta(days=a.lookback_days)


def test_a_short_history_holding_is_left_out_not_measured_over_its_own_interval(
    client, db_session, tenant, monkeypatch
):
    """XOM's cache starts 30 days into the window. Before: its 20-day return entered the sum
    next to 50-day returns, and coverage read 1.0."""
    late = _closes("XOM")[30:]
    a = _compute(client, db_session, tenant, monkeypatch, {"XOM": late})
    assert a.computable
    assert [(u.ticker, u.reason) for u in a.unattributed] == [("XOM", "short_history")]
    assert 0.0 < a.coverage < 1.0, "XOM's market value was not attributed"
    held = {e.sector: e for e in a.sectors if e.port_weight > 0}
    assert set(held) == {"Technology"}
    assert held["Technology"].port_weight == pytest.approx(1.0), "weights are over the attributed population"
    assert (a.start_date, a.as_of) == (FIRST, LAST), "one leg's short history does not move the window"


def test_a_stale_holding_is_left_out(client, db_session, tenant, monkeypatch):
    """Every other series runs ten sessions past XOM's last close (its refresh bar, LAST)."""
    longer = {s: _closes(s, n=60) for s in ("AAPL", "XLK", "XLE", "XLV", "XLI", "XLF", "XLY", "XLP",
                                             "XLU", "XLB", "XLRE", "XLC", "SPY")}
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    a = attribution.compute_attribution(
        db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 3, 1), do_backfill=True,
        price_source=_source(longer), sector_source=_sectors, benchmark_source=_bench,
    )
    assert a.computable
    assert a.as_of > LAST
    assert [(u.ticker, u.reason) for u in a.unattributed] == [("XOM", "stale")]
    assert a.coverage < 1.0


def test_as_of_is_the_stalest_required_leg_and_every_return_ends_there(client, db_session, tenant, monkeypatch):
    """XLE (a benchmark-weighted sector ETF) stops 10 bars early. Before: as_of was the
    freshest series (LAST) while XLE's return silently ended 10 days sooner."""
    xle = _closes("XLE")[:-10]
    end = xle[-1].bar_date
    a = _compute(client, db_session, tenant, monkeypatch, {"XLE": xle})
    assert a.computable
    assert a.as_of == end
    tech = next(e for e in a.sectors if e.sector == "Technology")
    assert tech.port_return == pytest.approx(_ret(_closes("AAPL"), FIRST, end))
    assert tech.bench_return == pytest.approx(_ret(_closes("XLK"), FIRST, end))


def test_a_classified_holding_without_a_window_return_does_not_count_toward_coverage(
    client, db_session, tenant, monkeypatch
):
    """XOM has no cached history but the refresh's single latest bar — no window return.
    Before: it still carried Energy's portfolio weight and counted as covered."""
    a = _compute(client, db_session, tenant, monkeypatch, {"XOM": []})
    assert [(u.ticker, u.reason) for u in a.unattributed] == [("XOM", "short_history")]
    assert a.coverage < 1.0
    assert all(e.port_weight == 0 for e in a.sectors if e.sector == "Energy")
    assert sum(e.port_weight for e in a.sectors) == pytest.approx(1.0)


def test_the_api_surfaces_the_window_and_what_was_left_out(client, tenant, monkeypatch):
    monkeypatch.setattr(settings, "feed_entitled", True)  # attribution is feed-gated
    pid = _seed(client, tenant)
    _refresh(client, tenant, pid, monkeypatch)
    real = attribution.compute_attribution

    def pinned(*args, **kwargs):
        # The route computes as of date.today(); pin the date and the injected sources.
        kwargs.update(today=TODAY, price_source=_source({"XOM": _closes("XOM")[30:]}),
                      sector_source=_sectors, benchmark_source=_bench)
        return real(*args, **kwargs)

    monkeypatch.setattr(attribution, "compute_attribution", pinned)
    body = client.post(f"/portfolios/{pid}/attribution/compute", headers={"X-Tenant-Id": tenant}).json()
    assert body["computable"] is True
    assert body["start_date"] == FIRST.isoformat() and body["as_of"] == LAST.isoformat()
    assert body["requested_start"] is not None
    assert [(u["ticker"], u["reason"]) for u in body["unattributed"]] == [("XOM", "short_history")]
