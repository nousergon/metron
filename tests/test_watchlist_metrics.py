"""Watchlist metrics (metron-ops#121) — a tracked-but-not-held ticker gets the SAME
valuation/fundamentals/technicals/consensus/attractiveness metrics the Holdings table
shows, via the shared ``metrics_enrichment`` pipeline, while remaining structurally
isolated from NAV/holdings/position economics.

Pure unit tests for the enrichment path (injected readers / monkeypatched yf map, no S3,
no DB) + one DB-backed API test proving a watchlist-only ticker never appears in Holdings.
"""

from __future__ import annotations

import io

from api.services import metrics_enrichment, watchlist


def test_watchlist_metrics_populate_via_data_spine_when_feed_entitled(db_session, monkeypatch):
    from api.db import models

    tenant = models.Tenant(name="t")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()
    watchlist.add_to_watchlist(db_session, tenant.id, pf.id, "NVDA")

    monkeypatch.setattr(
        metrics_enrichment.tearsheet_service, "_yf_symbol_map", lambda s, t: {"NVDA": "NVDA"}
    )

    class _Snap:
        def __init__(self, by):
            self.by_symbol = by
            self.by_sector = {}
            self.by_country = {}

    class _Fund:
        forward_pe = 30.0

        def __getattr__(self, _):
            return None

    monkeypatch.setattr(
        metrics_enrichment.fundamentals_service, "load_fundamentals", lambda: _Snap({"NVDA": _Fund()})
    )
    monkeypatch.setattr(metrics_enrichment.technicals_service, "load_technicals", lambda: _Snap({}))
    monkeypatch.setattr(metrics_enrichment.analyst_service, "load_analyst", lambda: _Snap({}))
    monkeypatch.setattr(metrics_enrichment.sentiment_service, "load_sentiment", lambda: _Snap({}))
    monkeypatch.setattr(
        metrics_enrichment.valuation_medians_service, "load_valuation_medians", lambda: _Snap({})
    )

    entries = watchlist.list_watchlist(db_session, tenant.id, pf.id, feed_entitled=True)
    assert len(entries) == 1
    e = entries[0]
    assert e.symbol == "NVDA"
    assert e.fwd_pe == 30.0
    # No live price for a watchlist ticker → upside stays a coverage gap, never fabricated.
    assert e.price_target_upside is None
    # No position economics ever attach to a watchlist entry.
    assert not hasattr(e, "quantity")
    assert not hasattr(e, "market_value")


def test_watchlist_metrics_off_feed_are_none(db_session):
    from api.db import models

    tenant = models.Tenant(name="t")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()
    watchlist.add_to_watchlist(db_session, tenant.id, pf.id, "NVDA")

    entries = watchlist.list_watchlist(db_session, tenant.id, pf.id, feed_entitled=False)
    assert len(entries) == 1
    e = entries[0]
    assert e.fwd_pe is None and e.attractiveness is None


def test_watchlist_only_ticker_never_appears_in_holdings_or_nav(client):
    tenant = "11111111-1111-1111-1111-111111111111"
    hdr = {"X-Tenant-Id": tenant}
    pid = client.post("/portfolios", json={"name": "P"}, headers=hdr).json()["id"]

    # A real holding (MSFT) so the portfolio has non-trivial NAV, plus a watchlist-only
    # ticker (TSLA) that is never bought.
    csv = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,MSFT,5,100,500,Brokerage\n"
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers=hdr,
    )
    client.post(f"/portfolios/{pid}/watchlist", json={"symbol": "TSLA"}, headers=hdr)

    holdings = client.get(f"/portfolios/{pid}/holdings", headers=hdr).json()
    tickers = {h["ticker"] for h in holdings}
    assert tickers == {"MSFT"}  # TSLA (watchlist-only) never appears among holdings

    summary = client.get(f"/portfolios/{pid}/summary", headers=hdr).json()
    assert summary["n_holdings"] == 1  # NAV/holdings-count sees only the real position
