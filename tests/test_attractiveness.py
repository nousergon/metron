"""Composite attractiveness score — SOTA 6-pillar cross-sectional blend.

Pins the NE factor-profile consumer contract: full-universe cross-section, per-pillar
lookup, graceful coverage gaps, and honest None for tickers outside the scanner universe.
"""

from __future__ import annotations

from datetime import date

from api.db import models
from api.services import (
    analytics,
    attractiveness,
    metrics_enrichment,
    tearsheet,
)

_PROFILES = {
    "AAPL": {
        "sector": "Information Technology",
        "quality_score": 90.0,
        "value_score": 30.0,
        "momentum_score": 85.0,
        "growth_score": 80.0,
        "stewardship_score": 70.0,
        "low_vol_score": 60.0,
    },
    "MSFT": {
        "sector": "Information Technology",
        "quality_score": 60.0,
        "value_score": 50.0,
        "momentum_score": 55.0,
        "growth_score": 45.0,
        "stewardship_score": 40.0,
        "low_vol_score": 35.0,
    },
}


def test_compute_universe_returns_cross_sectional_scores():
    universe = attractiveness.compute_universe(profiles_reader=lambda: _PROFILES)
    assert "AAPL" in universe and "MSFT" in universe
    assert universe["AAPL"].score is not None
    assert universe["AAPL"].coverage == 6
    assert {p.key for p in universe["AAPL"].pillars} == {
        "quality", "value", "momentum", "growth", "stewardship", "defensiveness",
    }


def test_lookup_misses_outside_universe():
    universe = attractiveness.compute_universe(profiles_reader=lambda: _PROFILES)
    assert attractiveness.lookup("ZZZ", universe) is None


def test_enrich_metrics_attaches_sota_attractiveness(db_session, monkeypatch):
    held = [
        analytics.Holding(
            ticker="AAPL", quantity=10.0, avg_cost=100.0, cost_basis=1000.0, currency="USD",
            last_price=200.0, sector="Technology",
        )
    ]
    monkeypatch.setattr(
        metrics_enrichment.tearsheet_service, "_yf_symbol_map", lambda s, t: {"AAPL": "AAPL"},
    )
    monkeypatch.setattr(metrics_enrichment.fundamentals_service, "load_fundamentals", lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals", lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst", lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment", lambda: type("S", (), {"by_symbol": {}})())

    def _test_profiles_reader():
        # load_factor_profiles(reader=...) parses a RAW dict into a snapshot itself —
        # a reader returning an already-built FactorProfilesSnapshot fails its
        # isinstance(raw, dict) check and silently yields an empty universe.
        return _PROFILES

    # Directly call the uncached computation with test profiles to avoid cache recursion
    original_compute_universe = attractiveness._compute_universe_uncached
    def _mock_compute_universe(profiles_reader=None, weights_reader=None):
        return original_compute_universe(
            profiles_reader=profiles_reader or _test_profiles_reader,
            weights_reader=weights_reader,
        )
    monkeypatch.setattr(attractiveness, "compute_universe", _mock_compute_universe)

    metrics_enrichment.enrich_metrics(db_session, held)
    aapl = held[0]
    assert aapl.attractiveness is not None
    assert aapl.attractiveness_coverage == 6
    assert aapl.attractiveness_quality == 90.0
    assert aapl.attractiveness_value == 30.0


def _seed_aapl(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD")
    aapl = models.Security(symbol="AAPL", currency="USD")
    session.add_all([acct, aapl])
    session.flush()
    session.add(models.Transaction(
        tenant_id=tenant.id, account_id=acct.id, security_id=aapl.id, txn_type="BUY",
        quantity=10, price=100.0, amount=1000.0, currency="USD",
        trade_date=date(2025, 1, 1), source_key="buy-aapl",
    ))
    session.add(models.PriceBar(security_id=aapl.id, bar_date=date(2025, 1, 2), close=200.0, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def test_tearsheet_gauge_populates_when_profiles_available(db_session, monkeypatch):
    tenant_id, pid = _seed_aapl(db_session)

    def _test_profiles_reader():
        # load_factor_profiles(reader=...) parses a RAW dict into a snapshot itself —
        # a reader returning an already-built FactorProfilesSnapshot fails its
        # isinstance(raw, dict) check and silently yields an empty universe.
        return _PROFILES

    # Directly call the uncached computation with test profiles to avoid cache recursion
    original_compute_universe = attractiveness._compute_universe_uncached
    def _mock_compute_universe(profiles_reader=None, weights_reader=None):
        return original_compute_universe(
            profiles_reader=profiles_reader or _test_profiles_reader,
            weights_reader=weights_reader,
        )
    monkeypatch.setattr(attractiveness, "compute_universe", _mock_compute_universe)

    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=True)
    att = sheet.attractiveness
    assert att.available is True
    assert att.score is not None
    assert att.coverage == 6
    assert {c.key for c in att.components} == {
        "quality", "value", "momentum", "growth", "stewardship", "defensiveness",
    }


def test_tearsheet_gauge_gated_off_when_feed_disabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=False)
    assert sheet.attractiveness.available is False
    assert sheet.attractiveness.score is None


def test_request_scoped_cache_deduplicates_within_request():
    """Multiple compute_universe() calls within the same request should read S3 once."""
    call_count = 0

    def _counting_profiles_reader():
        nonlocal call_count
        call_count += 1
        return _PROFILES

    # Clear all caches to start fresh
    attractiveness.clear_cache()

    # First call: computes fresh, increments call_count
    universe1 = attractiveness.compute_universe(profiles_reader=_counting_profiles_reader)
    assert call_count == 1

    # Second call in same request-context: should use request-scoped cache, NO S3 read
    universe2 = attractiveness.compute_universe(profiles_reader=_counting_profiles_reader)
    assert call_count == 1  # No increment — cache hit
    assert universe1 is not universe2  # Different dict objects (cache returns a copy)
    assert universe1 == universe2      # But same content

    # After clearing request cache, next call should read again
    attractiveness.clear_request_cache()
    universe3 = attractiveness.compute_universe(profiles_reader=_counting_profiles_reader)
    assert call_count == 2  # Incremented after request-cache clear
