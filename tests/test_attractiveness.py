"""Composite attractiveness score (metron-ops#106, Phase 2).

Pins the transparent scoring contract: the per-component transforms, the renormalize-over-
present-components blend, graceful coverage gaps (incl. the paid revision feed always absent),
and the no-signal → None rule. Also pins that the Holdings endpoint + tearsheet attach the
same score. Pure unit tests — injected readers / monkeypatched yf map, no S3, no DB.
"""

from __future__ import annotations

from datetime import date

from api.db import models
from api.routers import portfolios
from api.services import analytics, attractiveness, tearsheet

# ── component transforms ─────────────────────────────────────────────────────

def test_neutral_inputs_score_50():
    # Rating + sentiment at the neutral midpoint, fwd P/E exactly at the peer median,
    # zero upside → every present component is 0.5 → blended 50.0.
    att = attractiveness.compute(
        fwd_pe=20.0, median_fwd_pe=20.0,
        price_target_upside=0.0, consensus_score=0.0, news_sentiment=0.0,
    )
    assert att is not None
    assert att.score == 50.0
    assert att.coverage == 4  # revision absent (paid feed) → dropped


def test_cheap_high_upside_strong_buy_scores_high():
    att = attractiveness.compute(
        fwd_pe=10.0, median_fwd_pe=20.0,        # 50% discount → valuation sub 1.0
        price_target_upside=0.50,               # +50% → upside sub 1.0
        consensus_score=1.0,                    # strongBuy → rating sub 1.0
        news_sentiment=1.0,                     # max sentiment → sub 1.0
    )
    assert att is not None and att.score == 100.0


def test_rich_negative_signals_score_low():
    att = attractiveness.compute(
        fwd_pe=40.0, median_fwd_pe=20.0,        # 100% premium → clamps to 0.0
        price_target_upside=-0.80,              # large downside → clamps to 0.0
        consensus_score=-1.0,                   # strongSell → 0.0
        news_sentiment=-1.0,                    # min sentiment → 0.0
    )
    assert att is not None and att.score == 0.0


def test_negative_or_zero_forward_pe_drops_valuation_leg():
    # A non-positive fwd P/E is not a meaningful valuation signal → valuation component drops,
    # weights renormalize over the remaining components (here only rating).
    att = attractiveness.compute(fwd_pe=-5.0, median_fwd_pe=20.0, consensus_score=1.0)
    assert att is not None
    assert att.coverage == 1
    assert {c.key for c in att.components} == {"rating"}
    assert att.score == 100.0


def test_renormalizes_over_present_components():
    # Only upside present → its sub-score IS the blend, regardless of the catalog weight.
    att = attractiveness.compute(price_target_upside=0.25)  # +25% → 0.5 + 0.5*0.25/0.5 = 0.75
    assert att is not None
    assert att.coverage == 1 and att.score == 75.0


def test_no_components_returns_none():
    # Everything missing (off-feed / total coverage gap) → honest None, never a fabricated 50.
    assert attractiveness.compute() is None


def test_components_in_catalog_order_and_weights_reported():
    att = attractiveness.compute(
        news_sentiment=0.2, consensus_score=0.4, price_target_upside=0.1,
    )
    assert att is not None
    assert [c.key for c in att.components] == ["upside", "rating", "sentiment"]
    # Catalog weights surfaced verbatim for the inspectable gauge.
    by_key = {c.key: c.weight for c in att.components}
    assert by_key == {
        "upside": attractiveness.WEIGHTS["upside"],
        "rating": attractiveness.WEIGHTS["rating"],
        "sentiment": attractiveness.WEIGHTS["sentiment"],
    }


def test_weights_sum_to_one():
    assert abs(sum(attractiveness.WEIGHTS.values()) - 1.0) < 1e-9


# ── Holdings endpoint wiring ─────────────────────────────────────────────────

def _holding(ticker="AAPL"):
    h = analytics.Holding(
        ticker=ticker, quantity=10.0, avg_cost=100.0, cost_basis=1000.0, currency="USD"
    )
    h.last_price = 200.0
    h.sector = "Technology"
    return h


def test_enrich_metrics_attaches_attractiveness(db_session, monkeypatch):
    held = [_holding()]
    monkeypatch.setattr(
        portfolios.tearsheet_service, "_yf_symbol_map", lambda s, t: {"AAPL": "AAPL"}
    )

    class _Snap:
        def __init__(self, by):
            self.by_symbol = by
            self.as_of = date(2026, 6, 26)
            self.by_sector = by
            self.by_country = {}

    class _Fund:
        forward_pe = 15.0
        # everything else the enrich path reads → None-safe defaults
        def __getattr__(self, _):
            return None

    monkeypatch.setattr(
        portfolios.fundamentals_service, "load_fundamentals",
        lambda: _Snap({"AAPL": _Fund()}),
    )
    monkeypatch.setattr(portfolios.technicals_service, "load_technicals", lambda: _Snap({}))

    class _Analyst:
        consensus_rating = "buy"
        rating_score = 0.5
        mean_target = 240.0
        median_target = 235.0
        num_analysts = 30
        estimate_revision_trend = None
        def target_upside(self, price):
            return self.mean_target / price - 1.0

    monkeypatch.setattr(
        portfolios.analyst_service, "load_analyst", lambda: _Snap({"AAPL": _Analyst()})
    )

    class _Sent:
        sentiment = 0.3
        n_articles = 10

    monkeypatch.setattr(
        portfolios.sentiment_service, "load_sentiment", lambda: _Snap({"AAPL": _Sent()})
    )

    class _Median:
        forward_pe = 20.0

    monkeypatch.setattr(
        portfolios.valuation_medians_service, "load_valuation_medians",
        lambda: _Snap({"Technology": _Median()}),
    )

    portfolios._enrich_metrics(db_session, held)
    h = held[0]
    assert h.attractiveness is not None
    assert h.attractiveness_coverage == 4  # valuation+upside+rating+sentiment, revision absent
    # Cheaper than peers (15 vs 20), +20% upside, buy rating, +0.3 sentiment → clearly > 50.
    assert h.attractiveness > 50.0


# ── tearsheet gauge wiring (DB-backed) ───────────────────────────────────────

_ANALYST_ART = {
    "schema_version": 1, "as_of": "2026-06-26", "source": "yfinance",
    "analyst": {"AAPL": {"consensus_rating": "buy", "rating_score": 0.5,
                         "mean_target": 240.0, "median_target": 235.0, "num_analysts": 30}},
}
_SENTIMENT_ART = {
    "schema_version": 1, "as_of": "2026-06-26", "source": "lm",
    "sentiment": {"AAPL": {"sentiment": 0.3, "n_articles": 10, "as_of": "2026-06-25"}},
}


def _seed_aapl(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv",
                          external_id="CSV-1", currency="USD")
    aapl = models.Security(symbol="AAPL", currency="USD")
    session.add_all([acct, aapl])
    session.flush()
    session.add(models.Transaction(
        tenant_id=tenant.id, account_id=acct.id, security_id=aapl.id, txn_type="BUY",
        quantity=10, price=100.0, amount=1000.0, currency="USD",
        trade_date=date(2025, 1, 1), source_key="buy-aapl",
    ))
    session.add(models.PriceBar(security_id=aapl.id, bar_date=date(2025, 1, 2),
                                close=200.0, currency="USD"))
    session.commit()
    return tenant.id, pf.id


def test_tearsheet_gauge_populates_when_feed_enabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=True,
        analyst_reader=lambda: _ANALYST_ART, sentiment_reader=lambda: _SENTIMENT_ART,
    )
    # Fundamentals/medians artifacts are absent (default S3 reader fails soft → None), so the
    # gauge blends only the consensus legs: upside (+20%), rating (buy), sentiment (+0.3).
    att = sheet.attractiveness
    assert att.available is True
    assert att.coverage == 3
    assert {c.key for c in att.components} == {"upside", "rating", "sentiment"}
    assert att.score is not None and att.score > 50.0


def test_tearsheet_gauge_gated_off_when_feed_disabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=False,
        analyst_reader=lambda: _ANALYST_ART, sentiment_reader=lambda: _SENTIMENT_ART,
    )
    assert sheet.attractiveness.available is False
    assert sheet.attractiveness.score is None
