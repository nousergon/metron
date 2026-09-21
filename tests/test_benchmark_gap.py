"""Name-level benchmark-gap drivers (metron-ops-I346) — the "why" behind the
holdings-vs-index alpha ``performance.period_tiles`` already shows.

The correctness gate (deliverable 3, the single most important test in the issue) gets
its own dedicated tests: the decomposition must reconcile to the alpha already on
screen within tolerance, or the service refuses rather than showing plausible-looking
drivers. A second dedicated test locks in the percentage-point/fraction unit boundary
(``portfolio_analytics/index_contributions/source.py``) — the likeliest defect in the
whole feature.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from nousergon_lib.quant.attribution import security_contributions

from api.config import settings
from api.db import models
from api.services import benchmark_gap
from api.services.performance import BenchmarkReturn, PeriodTile
from portfolio_analytics.index_contributions.source import fetch_index_contributions

_START = date(2026, 9, 18)
_END = date(2026, 9, 21)


def _portfolio(session) -> models.Portfolio:
    tid = uuid.uuid4()
    session.add(models.Tenant(id=tid, name="t"))
    p = models.Portfolio(tenant_id=tid, name="P", base_currency="USD")
    session.add(p)
    session.flush()
    session.commit()
    return p


def _leg(ticker, qty, price, fx=1.0):
    return {"ticker": ticker, "qty": qty, "price": price, "fx_rate": fx, "currency": "USD", "value": qty * price}


def _snap(session, p, when, legs):
    session.add(
        models.NavSnapshot(
            tenant_id=p.tenant_id,
            portfolio_id=p.id,
            snap_date=when,
            nav=sum(leg["value"] for leg in legs),
            cost_basis=0,
            external_flow=0,
            composition={"schema": 1, "legs": legs},
        )
    )
    session.commit()


def _seed_portfolio(session):
    """A portfolio holding AAPL (60% prior-close weight, +2% today) and MSFT (40%, flat).
    Portfolio TWR ties out exactly to 0.6*0.02 + 0.4*0.00 = 0.012 by construction (no
    external flow), so the reconciliation math below is exact, not approximate."""
    p = _portfolio(session)
    _snap(session, p, _START, [_leg("AAPL", 60, 100.0), _leg("MSFT", 20, 200.0)])
    _snap(session, p, _END, [_leg("AAPL", 60, 102.0), _leg("MSFT", 20, 200.0)])
    return p


_ARTIFACT_RAW = {
    "schema_version": 1,
    "index": "SPX",
    "proxy_symbol": "SPY",
    "as_of": _END.isoformat(),
    "prior_close_date": _START.isoformat(),
    "index_return_pct": 1.2,
    "weight_method": "official",
    "residual_pp": 0.05,
    "coverage": {"weight_with_return": 0.08, "members": 2, "members_missing_return": 0},
    "constituents": [
        # weight * return(as a FRACTION) * 100 == contribution_pp (PERCENTAGE POINTS):
        # 0.07 * 0.02 * 100 = 0.14; 0.01 * 0.288 * 100 = 0.288.
        {"symbol": "AAPL", "weight_prior_close": 0.07, "return_pct": 0.02, "contribution_pp": 0.14},
        {"symbol": "APP", "weight_prior_close": 0.01, "return_pct": 0.288, "contribution_pp": 0.288},
    ],
}


def _source(index, as_of):
    assert index == "SPX"
    return dict(_ARTIFACT_RAW) if as_of == _END else None


def _tile(*, ret: float, alpha: float) -> PeriodTile:
    return PeriodTile(
        period="today", label="Today", start_date=_START, end_date=_END,
        gain=120.0, twr=0.012,
        benchmarks=[BenchmarkReturn(symbol="SPY", label="S&P 500", ret=ret, alpha=alpha)],
    )


class TestReconciliationGate:
    """Deliverable 3 — the single most important test in the issue."""

    def test_reconciling_decomposition_ranks_the_uncovered_name_first(self, db_session):
        p = _seed_portfolio(db_session)
        # benchmark_return chosen so portfolio_return(0.012) - benchmark_return exactly
        # equals the hand-computed sum of active contributions (0.00772) — residual 0.
        tile = _tile(ret=0.00428, alpha=0.00772)
        result = benchmark_gap.compute_benchmark_gap(
            db_session, p.tenant_id, p.id, index="SPX", tile=tile, source=_source,
        )
        assert result.computable is True
        assert result.within_tolerance is True
        assert result.residual == pytest.approx(0.0, abs=1e-9)
        assert result.active_return == pytest.approx(0.00772)
        symbols = [d.symbol for d in result.drivers]
        assert symbols[0] == "AAPL"  # largest |active_contribution|
        assert "APP" in symbols  # the held-elsewhere-not-here name — the AppLovin case
        app = next(d for d in result.drivers if d.symbol == "APP")
        assert app.classification == "not_held"
        assert app.port_weight == 0.0
        assert app.active_contribution == pytest.approx(-0.01 * 0.288)

    def test_residual_outside_tolerance_refuses_to_show_drivers(self, db_session):
        p = _seed_portfolio(db_session)
        # Same portfolio/artifact, but a benchmark_return that does NOT match the
        # hand-computed active-contribution sum — residual = 0.00428, well outside the
        # default 5bps tolerance.
        tile = _tile(ret=0.0, alpha=0.012)
        result = benchmark_gap.compute_benchmark_gap(
            db_session, p.tenant_id, p.id, index="SPX", tile=tile, source=_source,
        )
        assert result.computable is False
        assert result.within_tolerance is False
        assert result.drivers == []
        assert "residual" in result.reason.lower()
        assert "reconcile" in result.reason.lower() or "refus" in result.reason.lower()

    def test_earnings_tag_is_factual_not_generated(self, db_session):
        p = _seed_portfolio(db_session)
        tile = _tile(ret=0.00428, alpha=0.00772)
        result = benchmark_gap.compute_benchmark_gap(
            db_session, p.tenant_id, p.id, index="SPX", tile=tile, source=_source,
            earnings_source=lambda syms, **k: {"APP": _END} if "APP" in syms else {},
        )
        assert result.computable is True
        app = next(d for d in result.drivers if d.symbol == "APP")
        aapl = next(d for d in result.drivers if d.symbol == "AAPL")
        assert app.earnings is True
        assert aapl.earnings is False


class TestUnitBoundary:
    """portfolio_analytics/index_contributions — the percentage-point/fraction
    boundary the issue flags as the likeliest defect in the whole feature."""

    def test_return_pct_is_a_fraction_not_percentage_points(self):
        artifact = fetch_index_contributions("SPX", _END, source=_source)
        assert artifact is not None
        app = next(c for c in artifact.constituents if c.symbol == "APP")
        # return_pct used AS-IS (never divided by 100): 0.288 means +28.8%.
        assert app.return_pct == pytest.approx(0.288)
        # contribution_pp IS percentage points: divide by 100 for the fraction that
        # cross-checks against weight * return.
        assert app.contribution_fraction == pytest.approx(0.00288)
        assert app.contribution_fraction == pytest.approx(app.weight_prior_close * app.return_pct)

    def test_index_return_pct_and_residual_pp_convert_to_fractions(self):
        artifact = fetch_index_contributions("SPX", _END, source=_source)
        assert artifact.index_return_fraction == pytest.approx(0.012)
        assert artifact.residual_fraction == pytest.approx(0.0005)

    def test_security_contributions_bench_leg_matches_artifact_contribution_pp(self):
        """The library's own bench_contribution for a constituent must equal the
        artifact's contribution_pp/100 — the cross-check the module docstring promises."""
        artifact = fetch_index_contributions("SPX", _END, source=_source)
        bench_weights = {c.symbol: c.weight_prior_close for c in artifact.constituents}
        returns = {c.symbol: c.return_pct for c in artifact.constituents}
        result = security_contributions({}, bench_weights, returns)
        by_symbol = {c.symbol: c for c in result.contributions}
        for c in artifact.constituents:
            assert by_symbol[c.symbol].bench_contribution == pytest.approx(c.contribution_fraction)

    def test_missing_artifact_renders_unexplained_never_no_drivers(self, db_session):
        p = _seed_portfolio(db_session)
        tile = _tile(ret=0.00428, alpha=0.00772)
        result = benchmark_gap.compute_benchmark_gap(
            db_session, p.tenant_id, p.id, index="NDX", tile=tile,
            source=lambda index, as_of: None,
        )
        assert result.computable is False
        assert result.drivers == []
        assert "yet" in result.reason.lower() or "no" in result.reason.lower()


class TestGuards:
    def test_unknown_index_raises(self, db_session):
        p = _seed_portfolio(db_session)
        tile = _tile(ret=0.00428, alpha=0.00772)
        with pytest.raises(ValueError):
            benchmark_gap.compute_benchmark_gap(db_session, p.tenant_id, p.id, index="DJIA", tile=tile)

    def test_no_tile_renders_not_computable(self, db_session):
        p = _seed_portfolio(db_session)
        result = benchmark_gap.compute_benchmark_gap(db_session, p.tenant_id, p.id, index="SPX", tile=None)
        assert result.computable is False

    def test_account_scoped_request_degrades_honestly(self, db_session):
        p = _seed_portfolio(db_session)
        tile = _tile(ret=0.00428, alpha=0.00772)
        result = benchmark_gap.compute_benchmark_gap(
            db_session, p.tenant_id, p.id, index="SPX", tile=tile, source=_source,
            account_ids={uuid.uuid4()},
        )
        assert result.computable is False


class TestEndpoint:
    """``GET /portfolios/{id}/benchmark-gap/{index}`` — entitlement gate + the router's
    plumbing of the SAME today-tile ``period_tiles`` already computed into the service."""

    def _seed_via_client(self, client, tenant):
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        return pid

    def test_locked_without_feed_entitlement(self, client, monkeypatch):
        tenant = str(uuid.uuid4())
        monkeypatch.setattr(settings, "feed_entitled", False)
        pid = self._seed_via_client(client, tenant)
        resp = client.get(f"/portfolios/{pid}/benchmark-gap/SPX", headers={"X-Tenant-Id": tenant})
        assert resp.status_code == 200
        body = resp.json()
        assert body["computable"] is False
        assert body["required_tier"] is not None

    def test_unknown_index_404s_on_path_validation(self, client, monkeypatch):
        tenant = str(uuid.uuid4())
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = self._seed_via_client(client, tenant)
        resp = client.get(f"/portfolios/{pid}/benchmark-gap/DJIA", headers={"X-Tenant-Id": tenant})
        assert resp.status_code == 422

    def test_happy_path_reconciles_end_to_end(self, client, db_session, monkeypatch):
        tenant = str(uuid.uuid4())
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = self._seed_via_client(client, tenant)
        p = type("P", (), {"tenant_id": uuid.UUID(tenant), "id": uuid.UUID(pid)})()
        _snap(db_session, p, _START, [_leg("AAPL", 60, 100.0), _leg("MSFT", 20, 200.0)])
        _snap(db_session, p, _END, [_leg("AAPL", 60, 102.0), _leg("MSFT", 20, 200.0)])
        tile = _tile(ret=0.00428, alpha=0.00772)
        # The router computes `period_tiles` itself (mirroring the Overview tile exactly,
        # today_bench included) — patch it to the same worked example the service-level
        # tests use, so this test locks in the router's WIRING, not the math again.
        monkeypatch.setattr(
            "api.routers.portfolios.performance.period_tiles",
            lambda *a, **k: type("R", (), {"tiles": [tile]})(),
        )
        monkeypatch.setattr(
            "api.services.benchmark_gap.fetch_index_contributions",
            lambda index, as_of, source=None: fetch_index_contributions(index, as_of, source=_source),
        )
        resp = client.get(f"/portfolios/{pid}/benchmark-gap/SPX", headers={"X-Tenant-Id": tenant})
        assert resp.status_code == 200
        body = resp.json()
        assert body["computable"] is True
        assert body["within_tolerance"] is True
        assert body["proxy_symbol"] == "SPY"
        assert any(d["symbol"] == "AAPL" for d in body["drivers"])

    def test_requires_ownership(self, client):
        tenant = str(uuid.uuid4())
        pid = self._seed_via_client(client, tenant)
        resp = client.get(f"/portfolios/{pid}/benchmark-gap/SPX", headers={"X-Tenant-Id": str(uuid.uuid4())})
        assert resp.status_code == 404
