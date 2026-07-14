"""Concentration & diversification diagnostics (metron-ops-I167).

Engine invariants (pure math, no DB): HHI / effective-N / top-N NAV share /
max-position; benchmark deltas only when a benchmark exists (never fabricated);
unclassified sector/country market value lands in an explicit coverage bucket, never a
guessed one; user-target drift is a MECHANICAL evaluation of the user's own authored
targets (a position exactly AT the stated max is within the rule — strict inequality);
empty portfolio → not-computable WITH a reason.

API invariants: settled valuation only; as-of metadata sourced from the close dates the
valuation used; target-drift rows null until the user has authored targets (loaded via
the plugin capability seam); watchlist tickers structurally excluded; ownership 404.
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from api.config import settings
from portfolio_analytics.domain.diagnostics import (
    DiagnosticsPosition,
    StatedTargets,
    compute_diagnostics,
    normalize_benchmark_weights,
)
from portfolio_analytics.prices import ClosePoint

# ── engine: fixtures ──────────────────────────────────────────────────────────

BENCH = {"Technology": 0.40, "Energy": 0.05, "Healthcare": 0.15, "Financial Services": 0.40}


def _pos(ticker: str, mv: float, sector: str | None = None, country: str | None = None) -> DiagnosticsPosition:
    return DiagnosticsPosition(ticker=ticker, market_value=mv, sector=sector, country=country)


class TestConcentrationMath:
    def test_equal_weight_hhi_and_topn(self):
        # 4 equal positions: HHI = 4·0.25² = 0.25, effective N = 4, top-5 covers all.
        positions = [_pos(t, 100.0) for t in ("A", "B", "C", "D")]
        r = compute_diagnostics(positions)
        c = r.concentration
        assert c.n_positions == 4
        assert c.hhi == pytest.approx(0.25)
        assert c.effective_n == pytest.approx(4.0)
        assert c.top5_share == pytest.approx(1.0)
        assert c.top10_share == pytest.approx(1.0)
        assert c.max_position_weight == pytest.approx(0.25)

    def test_top5_and_top10_shares(self):
        # 12 positions: 2 heavy (200 each) + 10 light (60 each) → total 1000.
        positions = [_pos(f"H{i}", 200.0) for i in range(2)] + [_pos(f"L{i}", 60.0) for i in range(10)]
        c = compute_diagnostics(positions).concentration
        assert c.top5_share == pytest.approx((200 * 2 + 60 * 3) / 1000)
        assert c.top10_share == pytest.approx((200 * 2 + 60 * 8) / 1000)
        assert c.max_position_ticker in {"H0", "H1"}
        assert c.max_position_weight == pytest.approx(0.2)

    def test_single_holding_degenerate_portfolio(self):
        # One position IS the portfolio: HHI = 1, effective N = 1, every share = 1.
        c = compute_diagnostics([_pos("ONLY", 500.0)]).concentration
        assert c.n_positions == 1
        assert c.hhi == pytest.approx(1.0)
        assert c.effective_n == pytest.approx(1.0)
        assert c.top5_share == pytest.approx(1.0)
        assert c.max_position_ticker == "ONLY"
        assert c.max_position_weight == pytest.approx(1.0)

    def test_empty_portfolio_not_computable_with_reason(self):
        r = compute_diagnostics([])
        assert r.computable is False
        assert r.reason and "holdings" in r.reason.lower()
        assert r.concentration is None and r.sectors == () and r.target_drift is None

    def test_non_positive_market_value_is_a_contract_violation(self):
        with pytest.raises(ValueError, match="non-positive"):
            compute_diagnostics([_pos("BAD", 0.0)])


class TestSectorBenchmarkDelta:
    def test_deltas_and_unheld_benchmark_sectors(self):
        positions = [
            _pos("AAPL", 600.0, sector="Technology"),
            _pos("XOM", 400.0, sector="Energy"),
        ]
        r = compute_diagnostics(positions, benchmark_weights=BENCH)
        assert r.benchmark_available is True
        by = {s.sector: s for s in r.sectors}
        assert by["Technology"].weight == pytest.approx(0.6)
        assert by["Technology"].benchmark_weight == pytest.approx(0.40)
        assert by["Technology"].delta == pytest.approx(0.20)
        assert by["Energy"].delta == pytest.approx(0.4 - 0.05)
        # Unheld benchmark sectors appear as explicit underweights, not silently missing.
        assert by["Healthcare"].weight == 0.0
        assert by["Healthcare"].delta == pytest.approx(-0.15)

    def test_benchmark_absent_yields_none_deltas_never_fabricated(self):
        positions = [_pos("AAPL", 600.0, sector="Technology"), _pos("XOM", 400.0, sector="Energy")]
        r = compute_diagnostics(positions, benchmark_weights={})
        assert r.benchmark_available is False
        assert all(s.benchmark_weight is None and s.delta is None for s in r.sectors)
        # Portfolio-side weights still fully reported.
        assert {s.sector for s in r.sectors} == {"Technology", "Energy"}

    def test_unclassified_sector_bucket_no_delta(self):
        positions = [_pos("AAPL", 750.0, sector="Technology"), _pos("MYSTERY", 250.0, sector=None)]
        r = compute_diagnostics(positions, benchmark_weights=BENCH)
        by = {s.sector: s for s in r.sectors}
        assert by["Unclassified"].weight == pytest.approx(0.25)
        assert by["Unclassified"].benchmark_weight is None and by["Unclassified"].delta is None
        # The coverage bucket sorts last.
        assert r.sectors[-1].sector == "Unclassified"

    def test_sector_labels_fold_to_canonical_taxonomy(self):
        # A drift-variant label ("Information Technology") groups with the canonical one.
        positions = [
            _pos("AAPL", 500.0, sector="Technology"),
            _pos("MSFT", 300.0, sector="Information Technology"),
        ]
        r = compute_diagnostics(positions, benchmark_weights=BENCH)
        by = {s.sector: s for s in r.sectors}
        assert by["Technology"].weight == pytest.approx(1.0)

    def test_normalize_benchmark_weights_renormalizes_and_filters(self):
        w = normalize_benchmark_weights({"Technology": 0.3, "Energy": 0.1, "Not A Sector": 0.6})
        assert sum(w.values()) == pytest.approx(1.0)
        assert w["Technology"] == pytest.approx(0.75)
        assert "Not A Sector" not in w
        assert normalize_benchmark_weights({}) == {}
        assert normalize_benchmark_weights(None) == {}


class TestGeography:
    def test_us_intl_unclassified_split(self):
        positions = [
            _pos("AAPL", 500.0, country="United States"),
            _pos("SAP", 300.0, country="Germany"),
            _pos("MYSTERY", 200.0, country=None),
        ]
        geo = {g.bucket: g for g in compute_diagnostics(positions).geography}
        assert geo["US"].weight == pytest.approx(0.5)
        assert geo["International"].weight == pytest.approx(0.3)
        assert geo["Unclassified"].weight == pytest.approx(0.2)

    def test_unclassified_bucket_omitted_when_fully_classified(self):
        positions = [_pos("AAPL", 500.0, country="United States")]
        buckets = [g.bucket for g in compute_diagnostics(positions).geography]
        assert buckets == ["US", "International"]


class TestTargetDrift:
    def test_no_targets_authored_yields_null_section(self):
        r = compute_diagnostics([_pos("AAPL", 100.0)], targets=None)
        assert r.target_drift is None
        # An all-empty StatedTargets counts as "nothing authored" too.
        r = compute_diagnostics([_pos("AAPL", 100.0)], targets=StatedTargets())
        assert r.target_drift is None

    def test_max_position_breach_is_strict_inequality(self):
        # AAPL is EXACTLY at the 25% limit — the user's own rule says "max", so AT the
        # max is within it; only the 50% position breaches.
        positions = [_pos("AAPL", 250.0), _pos("NVDA", 500.0), _pos("KO", 250.0)]
        targets = StatedTargets(max_single_position=0.25)
        rows = [r for r in compute_diagnostics(positions, targets=targets).target_drift if r.kind == "max_position"]
        assert [(r.label, r.breach) for r in rows] == [("NVDA", True)]
        assert rows[0].target == pytest.approx(0.25)
        assert rows[0].actual == pytest.approx(0.5)

    def test_max_position_no_breach_reports_largest_position(self):
        positions = [_pos("AAPL", 300.0), _pos("KO", 200.0)]
        targets = StatedTargets(max_single_position=0.60)
        rows = [r for r in compute_diagnostics(positions, targets=targets).target_drift if r.kind == "max_position"]
        assert len(rows) == 1
        assert rows[0].label == "AAPL" and rows[0].breach is False
        assert rows[0].actual == pytest.approx(0.6)  # equality edge again: 0.6 == 0.6 → no breach

    def test_avoid_sector_present_and_absent(self):
        positions = [
            _pos("XOM", 300.0, sector="Energy"),
            _pos("CVX", 100.0, sector="Energy"),
            _pos("AAPL", 600.0, sector="Technology"),
        ]
        # "health care" exercises the canonical fold (user vocabulary ≠ spine labels).
        targets = StatedTargets(avoid_sectors=("Energy", "health care"))
        rows = {r.label: r for r in compute_diagnostics(positions, targets=targets).target_drift}
        energy = rows["Energy"]
        assert energy.breach is True
        assert energy.actual == pytest.approx(0.4)
        assert energy.detail == "XOM, CVX"  # heaviest offender first
        health = rows["Healthcare"]
        assert health.breach is False and health.actual == 0.0 and health.detail is None

    def test_allocation_pair_normalized_against_classified_split(self):
        # Targets 60/15 normalize to 80/20 within the pair; actual split is over
        # CLASSIFIED (US+Intl) value only — mirrors the advisor profile's semantics.
        positions = [
            _pos("AAPL", 700.0, country="United States"),
            _pos("SAP", 300.0, country="Germany"),
        ]
        targets = StatedTargets(target_allocation={"us_equity": 0.60, "international": 0.15})
        rows = {r.label: r for r in compute_diagnostics(positions, targets=targets).target_drift}
        us = rows["US share of classified holdings"]
        assert us.target == pytest.approx(0.8)
        assert us.actual == pytest.approx(0.7)
        assert us.breach is None  # drift row, not a boolean rule
        intl = rows["International share of classified holdings"]
        assert intl.target == pytest.approx(0.2)
        assert intl.actual == pytest.approx(0.3)

    def test_unmeasurable_allocation_keys_report_none_never_guess(self):
        positions = [_pos("AAPL", 100.0, country="United States")]
        targets = StatedTargets(target_allocation={"bonds": 0.20, "us_equity": 0.80})
        rows = {r.label: r for r in compute_diagnostics(positions, targets=targets).target_drift}
        assert rows["bonds"].actual is None and "not measurable" in rows["bonds"].detail
        # us_equity without its international counterpart isn't normalizable either.
        assert rows["us_equity"].actual is None and "international" in rows["us_equity"].detail

    def test_from_profile_block_extracts_only_mechanical_targets(self):
        block = {
            "strategy": "growth",  # suitability — must never surface
            "risk_tolerance": "high",
            "target_allocation": {"us_equity": 0.6, "international": 0.4},
            "sector_preferences": {"avoid": ["Energy"], "overweight": ["Technology"]},
            "max_single_position": 0.10,
        }
        t = StatedTargets.from_profile_block(block)
        assert t.target_allocation == {"us_equity": 0.6, "international": 0.4}
        assert t.max_single_position == pytest.approx(0.10)
        assert t.avoid_sectors == ("Energy",)
        assert not hasattr(t, "strategy") and not hasattr(t, "risk_tolerance")

    def test_from_profile_block_no_targets(self):
        assert StatedTargets.from_profile_block(None) is None
        assert StatedTargets.from_profile_block({}) is None
        assert StatedTargets.from_profile_block({"strategy": "value", "risk_tolerance": "low"}) is None


# ── API: settled valuation, as-of metadata, plugin-loaded targets ─────────────

CSV = (
    "date,type,symbol,quantity,price\n"
    "2024-01-01,BUY,AAPL,10,100\n"
    "2024-01-01,BUY,XOM,5,100\n"
)

_HELD = {"AAPL", "XOM"}
_CLOSE_DATE = date(2024, 2, 19)
_SECTORS = {"AAPL": "Technology", "XOM": "Energy"}
_COUNTRIES = {"AAPL": "United States"}  # XOM left unclassified on purpose


def _latest(symbols, *, source=None):
    return {s: ClosePoint(_CLOSE_DATE, 100.0) for s in symbols if s in _HELD}


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


def _patch_reference_sources(monkeypatch, bench=None):
    monkeypatch.setattr("api.services.sectors.fetch_sectors", lambda syms, *, source=None: {s: _SECTORS[s] for s in syms if s in _SECTORS})
    monkeypatch.setattr("api.services.countries.fetch_countries", lambda syms, *, source=None: {s: _COUNTRIES[s] for s in syms if s in _COUNTRIES})
    monkeypatch.setattr(
        "api.services.diagnostics.fetch_benchmark_sector_weights",
        lambda *, source=None: dict(bench) if bench else {},
    )


class TestDiagnosticsEndpoint:
    def test_settled_payload_with_as_of_and_benchmark(self, client, tenant, monkeypatch):
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        _patch_reference_sources(monkeypatch, bench={"Technology": 0.5, "Energy": 0.5})
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        assert d["computable"] is True
        # as-of metadata is the settled close date the valuation used — never the
        # request date (the card's freshness badge sources THIS field).
        assert d["as_of"] == _CLOSE_DATE.isoformat()
        assert d["base_currency"] == "USD"
        # AAPL 10×100 = 1000, XOM 5×100 = 500.
        assert d["total_market_value"] == pytest.approx(1500.0)
        by = {s["sector"]: s for s in d["sectors"]}
        assert by["Technology"]["weight"] == pytest.approx(2 / 3)
        assert d["benchmark_available"] is True
        assert by["Technology"]["delta"] == pytest.approx(2 / 3 - 0.5)
        geo = {g["bucket"]: g for g in d["geography"]}
        assert geo["US"]["weight"] == pytest.approx(2 / 3)
        assert geo["Unclassified"]["weight"] == pytest.approx(1 / 3)  # XOM: no country → honest gap
        assert d["concentration"]["hhi"] == pytest.approx((2 / 3) ** 2 + (1 / 3) ** 2)
        assert d["concentration"]["max_position_ticker"] == "AAPL"
        # No targets authored (no advisor plugin in the public build) → section is null.
        assert d["target_drift"] is None

    def test_benchmark_artifact_missing_degrades_honestly(self, client, tenant, monkeypatch):
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        _patch_reference_sources(monkeypatch, bench=None)  # entitled, but the spine has nothing
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        assert d["computable"] is True
        assert d["benchmark_available"] is False
        assert d["benchmark_reason"] == "unavailable"
        assert all(s["benchmark_weight"] is None and s["delta"] is None for s in d["sectors"])

    def test_benchmark_locked_by_entitlement_never_reads_spine(self, client, tenant, monkeypatch):
        # Core concentration analytics stay available on the no-feed beta; only the
        # benchmark columns lock, carrying the upsell tier.
        monkeypatch.setattr(settings, "feed_entitled", False)
        pid = _seed(client, tenant)
        _refresh_broker_free(client, tenant, pid, monkeypatch)
        def _boom(*, source=None):  # pragma: no cover - fails the test if reached
            raise AssertionError("benchmark spine read attempted while unentitled")
        monkeypatch.setattr("api.services.diagnostics.fetch_benchmark_sector_weights", _boom)
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        assert d["computable"] is True
        assert d["benchmark_available"] is False
        assert d["benchmark_reason"] in {"feed", "benchmark", "tier"}
        assert d["benchmark_required_tier"] is not None

    def test_target_drift_rendered_only_when_targets_authored(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        _patch_reference_sources(monkeypatch)
        # The plugin capability seam: a private advisor plugin returning the tenant's
        # stored investor_profile block (schema mirrored in web/lib/api.ts).
        block = {
            "strategy": "growth",  # suitability field — must never surface in the payload
            "max_single_position": 0.10,
            "sector_preferences": {"avoid": ["Energy"]},
        }
        monkeypatch.setattr("api.services.diagnostics.investor_targets", lambda session, tid: block)
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        rows = d["target_drift"]
        assert rows is not None
        kinds = {r["kind"] for r in rows}
        assert kinds == {"max_position", "avoid_sector"}
        aapl = next(r for r in rows if r["kind"] == "max_position")
        assert aapl["label"] == "AAPL" and aapl["breach"] is True  # 66.7% > the stated 10%
        energy = next(r for r in rows if r["kind"] == "avoid_sector")
        assert energy["label"] == "Energy" and energy["breach"] is True and "XOM" in energy["detail"]
        # Suitability fields never appear anywhere in the payload (metron-ops-I166).
        assert "growth" not in str(d)

    def test_watchlist_tickers_structurally_excluded(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        _patch_reference_sources(monkeypatch)
        assert client.post(
            f"/portfolios/{pid}/watchlist", json={"symbol": "NVDA"}, headers={"X-Tenant-Id": tenant}
        ).status_code == 201
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        assert d["concentration"]["n_positions"] == 2  # AAPL + XOM only — never the watchlist
        assert d["concentration"]["max_position_ticker"] != "NVDA"

    def test_unpriced_portfolio_not_computable_with_reason(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)  # never refreshed → no market value
        _patch_reference_sources(monkeypatch)
        d = client.get(f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": tenant}).json()
        assert d["computable"] is False
        assert d["reason"] and "holdings" in d["reason"].lower()
        assert d["as_of"] is None

    def test_requires_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.get(
            f"/portfolios/{pid}/diagnostics", headers={"X-Tenant-Id": str(uuid.uuid4())}
        ).status_code == 404


def _refresh_broker_free(client, tenant, pid, monkeypatch):
    """Seed cached closes WITHOUT the feed-gated refresh endpoint (which 403s when
    ``feed_entitled`` is off) — writes the price rows the settled valuation reads."""
    from api.db.session import get_session
    from api.main import app
    from api.services import prices as price_service

    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _latest)
    override = app.dependency_overrides[get_session]
    gen = override()
    session = next(gen)
    try:
        price_service.refresh_latest_prices(session, sorted(_HELD))
    finally:
        gen.close()
