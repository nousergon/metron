"""Consensus-research + news-sentiment consumer (metron-ops#105, Phase 1).

Pins the contract Metron consumes from the data spine: the analyst artifact
(`market_data/analyst/latest.json`, alpha-engine-data#497) → `TickerAnalyst` → Holding
fields, and the sentiment artifact (`market_data/sentiment/latest.json`, #499) →
`TickerSentiment` → Holding fields. Also: fail-soft (missing artifact → fields None, no
error), feed-gating, and the paid forward-estimate N/A scaffold (metron-ops#107).

Pure unit tests — injected readers / monkeypatched yf map, no S3, no DB.
"""

from __future__ import annotations

from datetime import date

from api.db import models
from api.services import analyst, analytics, metrics_enrichment, sentiment, tearsheet

# Artifact shapes mirror exactly what alpha-engine-data's collect_analyst / collect_sentiment
# write (the envelope is {schema_version, as_of, source, <map>}; per-symbol sub-dicts omit
# coverage-gap fields).
_ANALYST_ART = {
    "schema_version": 1,
    "as_of": "2026-06-26",
    "source": "yfinance+finnhub",
    "analyst": {
        "AAPL": {"consensus_rating": "buy", "rating_score": 0.5, "mean_target": 240.0,
                 "median_target": 235.0, "num_analysts": 38},
        # A partial coverage-gap symbol: rating present, targets omitted by the producer.
        "PARTIAL": {"consensus_rating": "hold", "rating_score": 0.0},
    },
}
_SENTIMENT_ART = {
    "schema_version": 1,
    "as_of": "2026-06-26",
    "source": "news_aggregates_daily(LM)",
    "sentiment": {
        "AAPL": {"sentiment": 0.31, "sentiment_mean": 0.27, "n_articles": 42,
                 "event_count": 3, "event_severity_max": 0.8, "as_of": "2026-06-25"},
    },
}


# ── analyst service round-trip ───────────────────────────────────────────────

def test_analyst_parses_artifact():
    snap = analyst.load_analyst(reader=lambda: _ANALYST_ART)
    a = snap.by_symbol["AAPL"]
    assert a.consensus_rating == "buy" and a.rating_score == 0.5
    assert a.mean_target == 240.0 and a.median_target == 235.0 and a.num_analysts == 38
    assert str(snap.as_of) == "2026-06-26"
    # Coverage-gap symbol: present fields parsed, omitted ones None (never fabricated).
    p = snap.by_symbol["PARTIAL"]
    assert p.consensus_rating == "hold" and p.mean_target is None and p.num_analysts is None


def test_analyst_target_upside_vs_live_price():
    a = analyst.load_analyst(reader=lambda: _ANALYST_ART).by_symbol["AAPL"]
    assert a.target_upside(200.0) == 240.0 / 200.0 - 1.0   # +20%
    assert a.target_upside(None) is None                    # no price → no upside
    assert a.target_upside(0) is None                       # guard divide-by-zero


def test_analyst_paid_estimates_scaffold_is_na():
    """Free artifact carries no forward estimates → the paid columns stay None / N/A
    (metron-ops#107), and estimates_available is False until the paid feed lands."""
    a = analyst.load_analyst(reader=lambda: _ANALYST_ART).by_symbol["AAPL"]
    assert a.forward_eps is None and a.forward_pe_consensus is None
    assert a.peg_consensus is None and a.estimate_revision_trend is None
    assert a.estimates_available is False
    assert analyst.PAID_ESTIMATES_REASON == "N/A · paid feed"


def test_analyst_estimates_available_when_paid_feed_lands():
    """Forward-compat: a future artifact carrying a forward estimate flips the gate with no
    schema change (the columns auto-populate)."""
    art = {"analyst": {"X": {"consensus_rating": "buy", "forward_eps": 7.5}}}
    a = analyst.load_analyst(reader=lambda: art).by_symbol["X"]
    assert a.forward_eps == 7.5 and a.estimates_available is True


# ── sentiment service round-trip ─────────────────────────────────────────────

def test_sentiment_parses_artifact():
    snap = sentiment.load_sentiment(reader=lambda: _SENTIMENT_ART)
    s = snap.by_symbol["AAPL"]
    assert s.sentiment == 0.31 and s.sentiment_mean == 0.27 and s.n_articles == 42
    assert s.event_count == 3 and s.event_severity_max == 0.8
    assert str(s.as_of) == "2026-06-25"          # per-ticker staleness anchor
    assert str(snap.as_of) == "2026-06-26"        # artifact run_date


# ── fail-soft: a missing artifact never errors ───────────────────────────────

def test_analyst_missing_artifact_is_empty():
    snap = analyst.load_analyst(reader=lambda: None)
    assert snap.by_symbol == {} and snap.as_of is None


def test_sentiment_missing_artifact_is_empty():
    snap = sentiment.load_sentiment(reader=lambda: None)
    assert snap.by_symbol == {} and snap.as_of is None


# ── per-holding enrichment mapping ───────────────────────────────────────────

def _patch_loaders(monkeypatch, *, analyst_art, sentiment_art):
    """Wire the spine loaders to injected artifacts; stub fundamentals/technicals empty so
    enrich_metrics exercises only the consensus/sentiment path."""
    real_analyst, real_sentiment = analyst.load_analyst, sentiment.load_sentiment
    monkeypatch.setattr(metrics_enrichment.tearsheet_service, "_yf_symbol_map",
                        lambda session, syms: {s: s for s in syms})
    monkeypatch.setattr(metrics_enrichment.fundamentals_service, "load_fundamentals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals",
                        lambda: type("S", (), {"by_symbol": {}})())
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst",
                        lambda: real_analyst(reader=lambda: analyst_art))
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment",
                        lambda: real_sentiment(reader=lambda: sentiment_art))


def test_enrich_metrics_maps_consensus_and_sentiment(monkeypatch):
    held = [
        analytics.Holding(ticker="AAPL", quantity=1, avg_cost=1, cost_basis=1, last_price=200.0),
        analytics.Holding(ticker="ZZZ", quantity=1, avg_cost=1, cost_basis=1),  # no spine data
    ]
    _patch_loaders(monkeypatch, analyst_art=_ANALYST_ART, sentiment_art=_SENTIMENT_ART)

    metrics_enrichment.enrich_metrics(session=None, held=held)

    aapl = held[0]
    assert aapl.consensus_rating == "buy" and aapl.consensus_score == 0.5
    assert aapl.price_target_mean == 240.0 and aapl.price_target_median == 235.0
    assert aapl.num_analysts == 38
    # Upside derived against the holding's live price (240/200 − 1 = +20%).
    assert aapl.price_target_upside == 240.0 / 200.0 - 1.0
    assert aapl.news_sentiment == 0.31 and aapl.news_articles == 42
    # A holding absent from both artifacts keeps every field None (coverage gap, not zeros).
    zzz = held[1]
    assert zzz.consensus_rating is None and zzz.price_target_upside is None
    assert zzz.news_sentiment is None and zzz.num_analysts is None


def test_enrich_metrics_fail_soft_on_missing_artifacts(monkeypatch):
    """Both artifacts missing → no error, all consensus/sentiment fields stay None."""
    held = [analytics.Holding(ticker="AAPL", quantity=1, avg_cost=1, cost_basis=1, last_price=200.0)]
    _patch_loaders(monkeypatch, analyst_art=None, sentiment_art=None)

    metrics_enrichment.enrich_metrics(session=None, held=held)

    h = held[0]
    assert h.consensus_rating is None and h.consensus_score is None
    assert h.price_target_mean is None and h.price_target_upside is None
    assert h.news_sentiment is None and h.news_articles is None


# ── tearsheet panel feed-gating (DB-backed) ──────────────────────────────────

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


def test_tearsheet_consensus_populates_when_feed_enabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=True,
        analyst_reader=lambda: _ANALYST_ART, sentiment_reader=lambda: _SENTIMENT_ART,
    )
    assert sheet.consensus_available is True
    assert sheet.consensus.consensus_rating == "buy"
    assert sheet.consensus.price_target_mean == 240.0
    # Upside vs the holding's last price (200 close) → +20%.
    assert sheet.consensus.price_target_upside == 240.0 / 200.0 - 1.0
    assert sheet.consensus.news_sentiment == 0.31 and sheet.consensus.news_articles == 42
    # Paid forward-estimate columns scaffolded N/A (metron-ops#107).
    assert sheet.consensus.estimates_available is False
    assert sheet.consensus.estimates_reason == "N/A · paid feed"
    assert sheet.consensus.forward_eps is None


def test_tearsheet_consensus_gated_off_when_feed_disabled(db_session):
    tenant_id, pid = _seed_aapl(db_session)
    sheet = tearsheet.tearsheet(
        db_session, tenant_id, pid, "AAPL", feed_enabled=False,
        analyst_reader=lambda: _ANALYST_ART, sentiment_reader=lambda: _SENTIMENT_ART,
    )
    assert sheet.consensus_available is False
    assert sheet.consensus.consensus_rating is None
    assert sheet.consensus.news_sentiment is None
