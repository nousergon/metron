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

    def _mock_profiles_reader():
        from api.services import factor_profiles as factor_profiles_service
        return factor_profiles_service.FactorProfilesSnapshot(as_of=None, by_ticker=_PROFILES)

    # Mock compute_universe to use test profiles; enrich_metrics calls compute_universe()
    # without custom readers, so we need to mock it at the call site.
    monkeypatch.setattr(
        metrics_enrichment.attractiveness_service, "compute_universe",
        lambda profiles_reader=None, weights_reader=None: attractiveness.compute_universe(
            profiles_reader=profiles_reader or _mock_profiles_reader,
            weights_reader=weights_reader,
        ),
    )

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

    def _mock_profiles_reader():
        from api.services import factor_profiles as factor_profiles_service
        return factor_profiles_service.FactorProfilesSnapshot(as_of=None, by_ticker=_PROFILES)

    from api.services import tearsheet as tearsheet_service
    # tearsheet.tearsheet calls attractiveness_service.compute_universe() without custom readers
    monkeypatch.setattr(
        tearsheet_service.attractiveness_service, "compute_universe",
        lambda profiles_reader=None, weights_reader=None: attractiveness.compute_universe(
            profiles_reader=profiles_reader or _mock_profiles_reader,
            weights_reader=weights_reader,
        ),
    )
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
