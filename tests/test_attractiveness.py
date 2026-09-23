"""Composite attractiveness score — SOTA 6-pillar cross-sectional blend.

Pins the NE factor-profile consumer contract: full-universe cross-section, per-pillar
lookup, graceful coverage gaps, and honest None for tickers outside the scanner universe.
"""

from __future__ import annotations

from datetime import date

from api.config import settings
from api.db import models
from api.services import (
    analytics,
    attractiveness,
    metrics_enrichment,
    tearsheet,
)
from api.services import (
    factor_profiles as factor_profiles_service,
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


def test_compute_universe_carries_factor_profile_as_of():
    # P-28 (data-collection-plan §7 R4): "Factor score" needs a daily as-of stamp — the
    # publish date is on the wrapped artifact envelope, not on a per-ticker profile.
    raw = {"as_of": "2026-09-10", "by_ticker": _PROFILES}
    universe = attractiveness.compute_universe(profiles_reader=lambda: raw)
    assert universe["AAPL"].as_of == date(2026, 9, 10)
    assert universe["MSFT"].as_of == date(2026, 9, 10)


def test_compute_universe_as_of_none_when_artifact_unwrapped():
    universe = attractiveness.compute_universe(profiles_reader=lambda: _PROFILES)
    assert universe["AAPL"].as_of is None


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
        return {"as_of": "2026-09-10", "by_ticker": _PROFILES}

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
    assert aapl.attractiveness_as_of == date(2026, 9, 10)  # P-28 daily as-of stamp


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
        return {"as_of": "2026-09-10", "by_ticker": _PROFILES}

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
    assert att.as_of == date(2026, 9, 10)  # P-28 daily as-of stamp
    assert {c.key for c in att.components} == {
        "quality", "value", "momentum", "growth", "stewardship", "defensiveness",
    }


def test_tearsheet_gauge_gated_off_when_feed_disabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=False)
    assert sheet.attractiveness.available is False
    assert sheet.attractiveness.score is None


def test_request_scoped_cache_deduplicates_within_request(monkeypatch):
    """Multiple compute_universe() calls within the same request should read S3 once.

    Supplying `profiles_reader` to compute_universe() intentionally bypasses ALL
    caching (see its docstring) — that's what lets test_compute_universe_* above get
    deterministic fresh results. So this test can't inject its counter via
    profiles_reader like the others; it has to mock the lower-level S3 read
    (factor_profiles_service.load_factor_profiles) and call compute_universe() with
    no reader at all, so the real request-scoped cache path actually executes.
    """
    call_count = 0

    def _counting_load_factor_profiles(*, reader=None):
        nonlocal call_count
        call_count += 1
        return factor_profiles_service.FactorProfilesSnapshot(as_of=None, by_ticker=_PROFILES)

    monkeypatch.setattr(attractiveness.factor_profiles_service, "load_factor_profiles", _counting_load_factor_profiles)

    # Clear all caches to start fresh
    attractiveness.clear_cache()

    # First call: computes fresh, increments call_count
    universe1 = attractiveness.compute_universe()
    assert call_count == 1

    # Second call in same request-context: should use request-scoped cache, NO S3 read
    universe2 = attractiveness.compute_universe()
    assert call_count == 1  # No increment — cache hit
    assert universe1 is universe2  # Request cache returns the exact stored object

    # Clearing only the request-scoped cache (simulating the next request) still
    # serves from the module-level 1-hour cache — no new S3 read, by design (this is
    # the whole point of having two tiers: request-scoped for within-request dedup,
    # module-level for across-request dedup within the TTL).
    attractiveness.clear_request_cache()
    universe3 = attractiveness.compute_universe()
    assert call_count == 1  # Still served by the module-level cache
    assert universe3 == universe1

    # Clearing BOTH caches forces a genuinely fresh read.
    attractiveness.clear_cache()
    attractiveness.compute_universe()
    assert call_count == 2  # Incremented after a full cache clear


# ── staleness + retirement (metron-ops-I308, Brian R4) ────────────────────────

def test_fresh_as_of_is_not_stale():
    raw = {"as_of": date.today().isoformat(), "by_ticker": _PROFILES}
    universe = attractiveness.compute_universe(profiles_reader=lambda: raw)
    assert universe["AAPL"].stale is False


def test_old_as_of_is_stale():
    from datetime import timedelta
    old_date = date.today() - timedelta(days=factor_profiles_service.STALE_AFTER_DAYS + 1)
    raw = {"as_of": old_date.isoformat(), "by_ticker": _PROFILES}
    universe = attractiveness.compute_universe(profiles_reader=lambda: raw)
    assert universe["AAPL"].stale is True


def test_missing_as_of_is_stale():
    universe = attractiveness.compute_universe(profiles_reader=lambda: _PROFILES)
    assert universe["AAPL"].stale is True  # unwrapped artifact carries no as_of


def test_retired_returns_empty_universe_without_reading_profiles(monkeypatch):
    monkeypatch.setattr(settings, "retired_v1_surfaces", True)
    calls = 0

    def _reader():
        nonlocal calls
        calls += 1
        return _PROFILES

    universe = attractiveness.compute_universe(profiles_reader=_reader)
    assert universe == {}
    assert calls == 0  # never reaches the S3 read
    assert attractiveness.retired() is True


# ── stale / retired reach the payload (metron-ops-I334) ───────────────────────
# The widgets that render the factor score inline must be able to tell three states apart
# from the payload alone: a per-ticker coverage gap (score None, not retired), a stale
# substrate (score kept, stale True), and a retired substrate (score None, retired True).


def _stub_enrichment_spine(monkeypatch, symbols):
    empty = lambda: type("S", (), {"by_symbol": {}})()  # noqa: E731
    monkeypatch.setattr(
        metrics_enrichment.tearsheet_service, "_yf_symbol_map", lambda s, t: {x: x for x in symbols},
    )
    monkeypatch.setattr(metrics_enrichment.fundamentals_service, "load_fundamentals", empty)
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals", empty)
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst", empty)
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment", empty)


def _universe_as_of(as_of: date | None):
    raw = {"as_of": as_of.isoformat(), "by_ticker": _PROFILES} if as_of else _PROFILES
    return attractiveness.compute_universe(profiles_reader=lambda: raw)


def _holding(ticker: str) -> analytics.Holding:
    return analytics.Holding(ticker=ticker, quantity=1.0, avg_cost=1.0, cost_basis=1.0)


def test_enrich_metrics_marks_stale_score_and_leaves_coverage_gap_plain(db_session, monkeypatch):
    from datetime import timedelta

    old = date.today() - timedelta(days=factor_profiles_service.STALE_AFTER_DAYS + 1)
    universe = _universe_as_of(old)
    _stub_enrichment_spine(monkeypatch, ["AAPL", "ZZZ"])
    monkeypatch.setattr(attractiveness, "compute_universe", lambda: universe)

    held = [_holding("AAPL"), _holding("ZZZ")]
    metrics_enrichment.enrich_metrics(db_session, held)
    aapl, zzz = held
    assert aapl.attractiveness is not None
    assert aapl.attractiveness_stale is True
    assert aapl.attractiveness_retired is False
    # Outside the scanner universe: an honest coverage gap, never flagged stale/retired.
    assert zzz.attractiveness is None
    assert zzz.attractiveness_stale is False
    assert zzz.attractiveness_retired is False


def test_enrich_metrics_fresh_score_is_not_stale(db_session, monkeypatch):
    universe = _universe_as_of(date.today())
    _stub_enrichment_spine(monkeypatch, ["AAPL"])
    monkeypatch.setattr(attractiveness, "compute_universe", lambda: universe)

    held = [_holding("AAPL")]
    metrics_enrichment.enrich_metrics(db_session, held)
    assert held[0].attractiveness is not None
    assert held[0].attractiveness_stale is False
    assert held[0].attractiveness_retired is False


def test_enrich_metrics_marks_every_row_retired_once_cut_over(db_session, monkeypatch):
    monkeypatch.setattr(settings, "retired_v1_surfaces", True)
    _stub_enrichment_spine(monkeypatch, ["AAPL", "ZZZ"])

    held = [_holding("AAPL"), _holding("ZZZ")]
    metrics_enrichment.enrich_metrics(db_session, held)
    for h in held:
        assert h.attractiveness is None
        assert h.attractiveness_retired is True
        assert h.attractiveness_stale is False


def test_holding_payload_carries_stale_and_retired():
    from api.routers.portfolios import HoldingOut, WatchlistEntryOut

    h = _holding("AAPL")
    h.attractiveness = 72.4
    h.attractiveness_stale = True
    out = HoldingOut.model_validate(h, from_attributes=True).model_dump()
    assert out["attractiveness_stale"] is True
    assert out["attractiveness_retired"] is False

    h.attractiveness = None
    h.attractiveness_stale = False
    h.attractiveness_retired = True
    out = HoldingOut.model_validate(h, from_attributes=True).model_dump()
    assert out["attractiveness_retired"] is True

    assert {"attractiveness_stale", "attractiveness_retired"} <= set(WatchlistEntryOut.model_fields)


def test_watchlist_entry_carries_stale_retired_and_pillars(db_session, monkeypatch):
    from datetime import timedelta

    from api.services import watchlist

    tenant = models.Tenant(name="t")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()
    watchlist.add_to_watchlist(db_session, tenant.id, pf.id, "AAPL")

    old = date.today() - timedelta(days=factor_profiles_service.STALE_AFTER_DAYS + 1)
    universe = _universe_as_of(old)
    real_compute_universe = attractiveness.compute_universe
    _stub_enrichment_spine(monkeypatch, ["AAPL"])
    monkeypatch.setattr(attractiveness, "compute_universe", lambda: universe)

    (e,) = watchlist.list_watchlist(db_session, tenant.id, pf.id, feed_entitled=True)
    assert e.attractiveness is not None
    assert e.attractiveness_stale is True
    assert e.attractiveness_retired is False
    assert e.attractiveness_quality == 90.0
    assert e.attractiveness_as_of == old

    # The real service returns {} once retired, before any S3 read.
    monkeypatch.setattr(attractiveness, "compute_universe", real_compute_universe)
    monkeypatch.setattr(settings, "retired_v1_surfaces", True)
    (e,) = watchlist.list_watchlist(db_session, tenant.id, pf.id, feed_entitled=True)
    assert e.attractiveness is None
    assert e.attractiveness_retired is True


def test_tearsheet_gauge_carries_stale(db_session, monkeypatch):
    from datetime import timedelta

    tenant_id, pid = _seed_aapl(db_session)
    old = date.today() - timedelta(days=factor_profiles_service.STALE_AFTER_DAYS + 1)
    universe = _universe_as_of(old)
    monkeypatch.setattr(attractiveness, "compute_universe", lambda: universe)

    att = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=True).attractiveness
    assert att.available is True
    assert att.score is not None
    assert att.stale is True
    assert att.retired is False


def test_tearsheet_gauge_retired_once_cut_over(db_session, monkeypatch):
    from api.routers.portfolios import TearsheetAttractivenessOut

    tenant_id, pid = _seed_aapl(db_session)
    monkeypatch.setattr(settings, "retired_v1_surfaces", True)

    att = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=True).attractiveness
    assert att.retired is True
    assert att.available is False
    assert att.score is None
    out = TearsheetAttractivenessOut.model_validate(att, from_attributes=True).model_dump()
    assert out["retired"] is True and out["stale"] is False


def test_tearsheet_gauge_retired_not_claimed_off_feed(db_session, monkeypatch):
    # Off-feed the gauge is gated (not entitled), which is not the same claim as retired.
    tenant_id, pid = _seed_aapl(db_session)
    monkeypatch.setattr(settings, "retired_v1_surfaces", True)
    att = tearsheet.tearsheet(db_session, tenant_id, pid, "AAPL", feed_enabled=False).attractiveness
    assert att.retired is False
