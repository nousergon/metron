"""Live intraday revaluation (metron-ops#79) — fresh NAV from current balances.

Service: overlays the data-spine intraday ``quotes`` (per-held-ticker last price) on the
EOD close, feed-gated, with per-symbol fallback (stale / missing / suspect → EOD). The
overlaid price map flows into ``valued_holdings`` / ``summary`` so the headline NAV
recomputes from live balances. Persistence (the daily snapshot) never sees intraday.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from api.config import settings
from api.db import models
from api.services import analytics, intraday

_AS_OF = "2026-06-12T15:00:00Z"
_NOW = datetime(2026, 6, 12, 15, 3, tzinfo=UTC)   # 3 min after the write — fresh
_STALE_NOW = datetime(2026, 6, 12, 15, 45, tzinfo=UTC)  # 45 min after — stale


def _art(quotes: dict) -> dict:
    return {"schema_version": 1, "as_of_utc": _AS_OF, "source": "yfinance_delayed", "quotes": quotes}


def _seed_one_holding(
    session, *, symbol="AAPL", yf_symbol="AAPL", qty=10, buy_px=100.0, eod_close=120.0,
    intraday_enabled=True,
):
    """One USD holding with a BUY (cost basis) + a cached EOD close bar. Enables the intraday
    overlay toggle by default — the overlay-mechanics tests below assume intraday is ON; the
    gate itself is covered by ``TestIntradayToggle``."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
    if intraday_enabled:
        session.add(
            models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=pf.id, intraday_enabled=True)
        )
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="A1", currency="USD"
    )
    sec = models.Security(symbol=symbol, yf_symbol=yf_symbol, currency="USD")
    session.add_all([acct, sec])
    session.flush()
    session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            txn_type="BUY", quantity=qty, price=buy_px, amount=qty * buy_px, currency="USD",
            trade_date=date(2024, 1, 1), source_key="buy-1",
        )
    )
    session.add(
        models.PriceBar(security_id=sec.id, bar_date=date(2026, 6, 11), close=eod_close, currency="USD")
    )
    session.commit()
    return tenant.id, pf.id


class TestLoadQuotes:
    def test_fresh_quotes(self):
        quotes, as_of, stale = intraday.load_quotes(reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW)
        assert quotes == {"AAPL": {"last": 130.0}} and as_of == _AS_OF and stale is False

    def test_stale_when_old(self):
        _, _, stale = intraday.load_quotes(reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_STALE_NOW)
        assert stale is True

    def test_missing_artifact(self):
        quotes, as_of, stale = intraday.load_quotes(reader=lambda: None, now=_NOW)
        assert quotes == {} and as_of is None and stale is True


class TestLivePrices:
    def test_not_applied_without_feed(self, db_session):
        tid, pid = _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=False, reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW
        )
        assert prices is None and meta.applied is False and meta.reason == "feed"

    def test_not_applied_when_stale(self, db_session):
        _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_STALE_NOW,
        )
        assert prices is None and meta.applied is False and meta.reason == "stale"

    def test_overlay_merges_intraday_over_eod(self, db_session):
        _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0, "session_date": "2026-06-12"}}), now=_NOW,
        )
        assert meta.applied is True and meta.n_priced == 1 and meta.n_total == 1
        assert prices["AAPL"].close == 130.0  # intraday last, not the 120 EOD close
        assert prices["AAPL"].bar_date == date(2026, 6, 12)

    def test_partial_coverage_disclosed(self, db_session):
        """Only one of two held tickers gets a usable quote — the other silently keeps its
        EOD close inside the same valuation, so the meta must disclose the partial live
        coverage (n_priced < n_total drives the UI's "n/N live" label, metron-ops#146)."""
        _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL", "MSFT"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW,
        )
        assert meta.applied is True
        assert meta.n_priced == 1 and meta.n_total == 2

    def test_suspect_quote_skipped(self, db_session):
        _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 999.0, "suspect": True}}), now=_NOW,
        )
        assert prices is None and meta.applied is False  # no usable quote → EOD valuation

    def test_missing_last_skipped(self, db_session):
        _seed_one_holding(db_session)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"prev_close": 120.0}}), now=_NOW,
        )
        assert prices is None and meta.applied is False

    def test_wrong_scale_quote_falls_back_to_settled_close(self, db_session):
        """Cross-source scale coherence (the 2026-07-08 MARUY incident): the quote feed
        returned 30.17 for a security whose settled close — and broker valuation — was
        308.40 (an ADR-ratio scale mismatch the producer's own-tick move guard cannot see).
        Such a quote must be unusable: the position keeps the settled close (disclosed as
        last_close), never a silently 10x-wrong live NAV."""
        _seed_one_holding(db_session, eod_close=308.40)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 30.17}}), now=_NOW,
        )
        # The only held ticker's quote is incoherent → no overlay applies at all.
        assert prices is None and meta.applied is False

    def test_wrong_scale_quote_excluded_but_others_overlay(self, db_session):
        """The coherence guard is per-symbol: the wrong-scaled ticker keeps its settled
        close (source=last_close) while a coherent quote still overlays."""
        _seed_one_holding(db_session, eod_close=308.40)  # AAPL plays the MARUY role
        prices, meta = intraday.live_prices(
            db_session, ["AAPL", "MSFT"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 30.17}, "MSFT": {"last": 130.0}}), now=_NOW,
        )
        assert meta.applied is True and meta.n_priced == 1 and meta.n_total == 2
        assert prices["AAPL"].close == 308.40  # settled close kept, not the wrong-scale quote
        assert prices["MSFT"].close == 130.0
        assert meta.source_by_ticker["AAPL"] == intraday.SOURCE_LAST_CLOSE
        assert meta.source_by_ticker["MSFT"] == intraday.SOURCE_DELAYED

    def test_large_but_coherent_move_still_overlays(self, db_session):
        """A big real move inside the bounds (+40%) is NOT excluded — the guard targets
        scale classes (10x/100x), not volatility."""
        _seed_one_holding(db_session, eod_close=100.0)
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 140.0}}), now=_NOW,
        )
        assert meta.applied is True and prices["AAPL"].close == 140.0


def _fund_art(fund_proxies: dict, quotes: dict | None = None) -> dict:
    return {
        "schema_version": 2,
        "as_of_utc": _AS_OF,
        "source": "yfinance_delayed",
        "quotes": quotes or {},
        "fund_proxies": fund_proxies,
    }


class TestOverlayFundEstimate:
    """Late-striking mutual-fund same-day ESTIMATE (metron-ops#112, mechanism B): a held
    ticker with NO usable intraday quote of its own but known to be a tracked fund
    (``fund_proxy.FUND_PROXY``) gets ``eod_close * (1 + proxy_return)`` synthesized from
    its tracking-proxy ETF's same-day return, and is named in the returned estimated set."""

    def test_synthesizes_price_from_proxy_return(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="FNILX", yf_symbol="FNILX", eod_close=20.0)
        # FNILX has no quote of its own; SPY (its proxy) is +2% today.
        art = _fund_art({"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": "2026-06-12"}})
        prices, meta = intraday.live_prices(
            db_session, ["FNILX"], feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        assert meta.applied is True
        assert "FNILX" in meta.estimated_tickers
        assert prices["FNILX"].close == pytest.approx(20.0 * 1.02)
        assert prices["FNILX"].bar_date == date(2026, 6, 12)  # today's session, not the EOD bar_date

    def test_non_fund_ticker_never_estimated(self, db_session):
        _seed_one_holding(db_session, symbol="AAPL", eod_close=120.0)
        art = _fund_art({"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": "2026-06-12"}})
        prices, meta = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        # AAPL has no quote AND isn't a known fund → no estimate, no overlay at all.
        assert prices is None and meta.applied is False
        assert meta.estimated_tickers == frozenset()

    def test_real_quote_wins_over_estimate(self, db_session):
        """A fund ticker WITH a usable intraday quote of its own is never estimated —
        the real quote always takes priority."""
        _seed_one_holding(db_session, symbol="FNILX", yf_symbol="FNILX", eod_close=20.0)
        art = _fund_art(
            {"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": "2026-06-12"}},
            quotes={"FNILX": {"last": 21.0, "session_date": "2026-06-12"}},
        )
        prices, meta = intraday.live_prices(
            db_session, ["FNILX"], feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        assert prices["FNILX"].close == 21.0  # the real quote, not an estimate
        assert "FNILX" not in meta.estimated_tickers

    def test_skipped_when_proxy_return_unavailable(self, db_session):
        """Wrong-session / missing proxy quote → proxy_day_return is None → the fund is
        left un-overlaid (falls back to EOD close), never a fabricated estimate."""
        _seed_one_holding(db_session, symbol="FNILX", yf_symbol="FNILX", eod_close=20.0)
        art = _fund_art({})  # no SPY proxy quote at all
        prices, meta = intraday.live_prices(
            db_session, ["FNILX"], feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        assert prices is None and meta.applied is False
        assert "FNILX" not in meta.estimated_tickers

    def test_skipped_when_no_eod_close(self, db_session):
        """A fund with no cached EOD close has nothing to scale the proxy return off of —
        skipped, never fabricated from nothing."""
        tenant = models.Tenant(name="t")
        db_session.add(tenant)
        db_session.flush()
        pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
        db_session.add(pf)
        db_session.flush()
        db_session.add(models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=pf.id, intraday_enabled=True))
        acct = models.Account(tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="A1", currency="USD")
        sec = models.Security(symbol="FNILX", yf_symbol="FNILX", currency="USD")
        db_session.add_all([acct, sec])
        db_session.flush()
        db_session.add(
            models.Transaction(
                tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                txn_type="BUY", quantity=5, price=20.0, amount=100.0, currency="USD",
                trade_date=date(2024, 1, 1), source_key="buy-1",
            )
        )
        db_session.commit()  # no PriceBar seeded — no EOD close cached
        art = _fund_art({"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": "2026-06-12"}})
        prices, meta = intraday.for_portfolio(
            db_session, tenant.id, pf.id, feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        assert prices is None and meta.applied is False


class TestLiveValuation:
    def test_nav_recomputes_from_intraday(self, db_session):
        tid, pid = _seed_one_holding(db_session, qty=10, eod_close=120.0)
        # EOD NAV = 10 × 120 = 1200.
        eod = analytics.summary(db_session, tid, pid)
        assert eod.market_value == 1200.0
        # Live NAV = 10 × 130 (intraday last).
        prices, _ = intraday.live_prices(
            db_session, ["AAPL"], feed_entitled=True, reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW
        )
        live = analytics.summary(db_session, tid, pid, prices=prices)
        assert live.market_value == 1300.0

    def test_persistence_path_stays_eod(self, db_session):
        """valued_holdings with no override (the snapshot path) always uses EOD close —
        intraday never enters the recorded NAV history."""
        tid, pid = _seed_one_holding(db_session, qty=10, eod_close=120.0)
        held = analytics.valued_holdings(db_session, tid, pid)  # default = EOD
        assert held[0].market_value == 1200.0


class TestTodayView:
    def test_decomposition_and_reconciliation(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", qty=10, eod_close=120.0)
        # prev_close 100, open 110, last 130 → overnight +10, intraday +20, day +30 (native).
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}}),
        )
        assert t.available and t.n_priced == 1 and t.n_excluded == 0
        r = t.rows[0]
        assert r.overnight_pct == 0.1 and r.intraday_pct == pytest.approx(20 / 110) and r.day_pct == 0.3
        assert r.overnight_gain == 100.0 and r.intraday_gain == 200.0 and r.day_gain == 300.0
        # Reconciliation: overnight $ + intraday $ == day $ (per-row and portfolio total).
        assert r.overnight_gain + r.intraday_gain == r.day_gain
        assert t.overnight_gain + t.intraday_gain == t.day_gain == 300.0
        # Portfolio %s are leg$ / prior-close MV (10 × 100 = 1000).
        assert t.day_pct == 0.3 and t.overnight_pct == 0.1

    def test_feed_off(self, db_session):
        tid, pid = _seed_one_holding(db_session)
        t = intraday.today_view(db_session, tid, pid, feed_entitled=False, reader=lambda: _art({}), now=_NOW)
        assert t.available is False and t.reason == "feed"

    def test_excludes_holding_without_quote(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", qty=10)
        # Quote for a different symbol → AAPL has no decomposable quote → excluded.
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"MSFT": {"prev_close": 1.0, "open": 1.0, "last": 1.0}}),
        )
        assert t.available and t.n_priced == 0 and t.n_excluded == 1 and t.day_gain is None

    def test_stale_snapshot_still_renders_as_of_close(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", qty=10)
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_STALE_NOW,
            reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}}),
        )
        assert t.available and t.stale is True and t.n_priced == 1  # rows still show, flagged stale


class TestIntradayToggle:
    """The single user-facing intraday switch (InvestorPreferences.intraday_enabled),
    default OFF — opt-in overlay; EOD-close valuation is authoritative until turned on."""

    def test_default_off_no_pref_row(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", intraday_enabled=False)
        assert intraday.intraday_enabled(db_session, tid, pid) is False
        prices, meta = intraday.for_portfolio(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"AAPL": {"last": 130.0}}),
        )
        assert prices is None and meta.applied is False and meta.reason == "off"

    def test_today_view_off_when_toggle_off(self, db_session):
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", intraday_enabled=False)
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 110.0, "last": 130.0}}),
        )
        assert t.available is False and t.reason == "off"

    def test_overlay_applies_when_toggle_on(self, db_session):
        # Seeder enables the toggle by default → the overlay is in effect.
        tid, pid = _seed_one_holding(db_session, symbol="AAPL", eod_close=120.0)
        assert intraday.intraday_enabled(db_session, tid, pid) is True
        prices, meta = intraday.for_portfolio(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"AAPL": {"last": 130.0}}),
        )
        assert meta.applied is True and prices["AAPL"].close == 130.0

    def test_feed_off_beats_toggle_on(self, db_session):
        # Deployment axis wins: no feed → "feed", even with the user toggle on.
        tid, pid = _seed_one_holding(db_session, symbol="AAPL")
        prices, meta = intraday.for_portfolio(
            db_session, tid, pid, feed_entitled=False, now=_NOW,
            reader=lambda: _art({"AAPL": {"last": 130.0}}),
        )
        assert prices is None and meta.reason == "feed"


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


class TestIntradayStatusEndpoint:
    def test_feed_off_reports_not_applied(self, client, tenant, monkeypatch):
        monkeypatch.setattr(settings, "feed_entitled", False)
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        r = client.get(f"/portfolios/{pid}/intraday", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False and body["reason"] == "feed"

    def test_feed_on_no_holdings_unavailable(self, client, tenant, monkeypatch):
        monkeypatch.setattr(settings, "feed_entitled", True)
        # No holdings → nothing to overlay; wiring still returns a clean status (not 500).
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        # Turn the intraday toggle on so we exercise the no-holdings path (not the off gate).
        client.put(
            f"/portfolios/{pid}/preferences",
            json={"intraday_enabled": True}, headers={"X-Tenant-Id": tenant},
        )
        r = client.get(f"/portfolios/{pid}/intraday", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        assert r.json()["applied"] is False

    def test_toggle_off_reports_off_even_with_feed(self, client, tenant, monkeypatch):
        # Feed entitled, but the user's single intraday switch is off (default) → reason "off".
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        r = client.get(f"/portfolios/{pid}/intraday", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False and body["reason"] == "off"
        # Coverage fields (metron-ops#146) are additive with safe defaults, and the
        # session state (metron-ops-I156) defaults to the conservative "closed".
        assert body["n_total"] == 0 and body["n_estimated"] == 0
        assert body["session_state"] == "closed"


def _seed_portfolio_multi(session, specs, *, intraday_enabled=True, base="USD"):
    """N holdings in one portfolio/account — ``specs`` is a list of dicts with ``symbol``,
    ``qty``, ``buy_px``, optional ``eod_close`` (None = no cached close bar) and
    ``currency``. The metron-ops#152 pricing-source / coverage tests need mixed-coverage
    portfolios the single-holding seeder can't express."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency=base)
    session.add(pf)
    session.flush()
    if intraday_enabled:
        session.add(
            models.InvestorPreferences(tenant_id=tenant.id, portfolio_id=pf.id, intraday_enabled=True)
        )
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="A1", currency=base
    )
    session.add(acct)
    session.flush()
    for i, s in enumerate(specs):
        ccy = s.get("currency", "USD")
        sec = models.Security(symbol=s["symbol"], yf_symbol=s.get("yf_symbol", s["symbol"]), currency=ccy)
        session.add(sec)
        session.flush()
        session.add(
            models.Transaction(
                tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
                txn_type="BUY", quantity=s["qty"], price=s["buy_px"], amount=s["qty"] * s["buy_px"],
                currency=ccy, trade_date=date(2024, 1, 1), source_key=f"buy-{i}",
            )
        )
        if s.get("eod_close") is not None:
            session.add(
                models.PriceBar(security_id=sec.id, bar_date=date(2026, 6, 11), close=s["eod_close"], currency=ccy)
            )
    session.commit()
    return tenant.id, pf.id


class TestPricingSources:
    """Per-position pricing source (metron-ops#152) — DERIVED from quote availability +
    freshness, never a manual per-account flag. The covered live basis is exactly
    {delayed, estimated}; {last_close, unpriced} fall back and must be disclosed."""

    def test_full_enum_classification(self, db_session):
        # AAPL: usable quote → delayed. MSFT: no quote, cached EOD close → last_close.
        # NOPX: no quote AND no EOD close → unpriced.
        _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 100.0, "eod_close": 120.0},
            {"symbol": "MSFT", "qty": 1, "buy_px": 100.0, "eod_close": 100.0},
            {"symbol": "NOPX", "qty": 1, "buy_px": 100.0, "eod_close": None},
        ])
        _, meta = intraday.live_prices(
            db_session, ["AAPL", "MSFT", "NOPX"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW,
        )
        assert meta.applied is True
        assert meta.source_by_ticker == {
            "AAPL": intraday.SOURCE_DELAYED,
            "MSFT": intraday.SOURCE_LAST_CLOSE,
            "NOPX": intraday.SOURCE_UNPRICED,
        }

    def test_synthesized_fund_estimate_source(self, db_session):
        _seed_portfolio_multi(db_session, [{"symbol": "FNILX", "qty": 5, "buy_px": 20.0, "eod_close": 20.0}])
        art = _fund_art({"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": "2026-06-12"}})
        _, meta = intraday.live_prices(
            db_session, ["FNILX"], feed_entitled=True, reader=lambda: art, now=_NOW,
        )
        assert meta.source_by_ticker == {"FNILX": intraday.SOURCE_ESTIMATED}


class TestNavWeightedCoverage:
    """``weigh_coverage`` (metron-ops#152): coverage is NAV-weighted — a ticker count
    misstates partial coverage whenever position sizes differ."""

    def test_nav_weighted_not_ticker_counted(self, db_session):
        # AAPL $900 of MV (covered), MSFT $100 (no quote): 90% of NAV covered while the
        # ticker count reads 1/2 — the count and the NAV fraction must disagree here.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 9, "buy_px": 50.0, "eod_close": 100.0},
            {"symbol": "MSFT", "qty": 1, "buy_px": 50.0, "eod_close": 100.0},
        ])
        prices, meta = intraday.live_prices(
            db_session, ["AAPL", "MSFT"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 100.0}}), now=_NOW,
        )
        held = analytics.valued_holdings(db_session, tid, pid, prices=prices)
        intraday.weigh_coverage(meta, held)
        assert meta.n_priced == 1 and meta.n_total == 2
        assert meta.covered_nav == 900.0 and meta.total_nav == 1000.0

    def test_covered_and_total_in_live_nav_terms(self, db_session):
        # The covered leg is valued at the intraday last (130), not the EOD close (120) —
        # covered/total must be in the SAME live-NAV terms as the headline NAV.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 100.0, "eod_close": 120.0},
            {"symbol": "MSFT", "qty": 10, "buy_px": 100.0, "eod_close": 100.0},
        ])
        prices, meta = intraday.live_prices(
            db_session, ["AAPL", "MSFT"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW,
        )
        held = analytics.valued_holdings(db_session, tid, pid, prices=prices)
        intraday.weigh_coverage(meta, held)
        assert meta.covered_nav == 1300.0        # 10 × 130 (live), not 10 × 120 (EOD)
        assert meta.total_nav == 2300.0          # + 10 × 100 at last close

    def test_no_fx_downgrades_source_to_unpriced(self, db_session):
        # A foreign holding with no cached FX rate has no base-currency MV — it enters
        # neither NAV figure, and its source is downgraded so the disclosure never claims
        # coverage the NAV math didn't include.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 100.0, "eod_close": 120.0},
            {"symbol": "0005.HK", "qty": 10, "buy_px": 50.0, "eod_close": 60.0, "currency": "HKD"},
        ])
        prices, meta = intraday.live_prices(
            db_session, ["AAPL", "0005.HK"], feed_entitled=True,
            reader=lambda: _art({"AAPL": {"last": 130.0}}), now=_NOW,
        )
        assert meta.source_by_ticker["0005.HK"] == intraday.SOURCE_LAST_CLOSE  # pre-valuation
        held = analytics.valued_holdings(db_session, tid, pid, prices=prices)
        intraday.weigh_coverage(meta, held)
        assert meta.source_by_ticker["0005.HK"] == intraday.SOURCE_UNPRICED   # no FX → not in NAV
        assert meta.covered_nav == 1300.0 and meta.total_nav == 1300.0

    def test_not_applied_meta_passes_through(self, db_session):
        meta = intraday.IntradayMeta(applied=False, reason="off")
        out = intraday.weigh_coverage(meta, [])
        assert out.covered_nav is None and out.total_nav is None


class TestCoveredBasisConvention:
    """The covered-basis rule (metron-ops#152): exclusion is by OBSERVABILITY (no usable
    quote), never by movement. A priced-but-flat holding stays in the denominator; an
    unquoted holding is in neither the numerator nor the denominator — and is named."""

    def test_flat_quoted_position_stays_in_denominator(self, db_session):
        # AAPL +10% on $1000 prev MV; MSFT quoted but flat on $1000 prev MV.
        # Portfolio day% = 100/2000 = 5% — the flat position correctly dilutes, because
        # a real 0% move is information, not a coverage gap.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
            {"symbol": "MSFT", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
        ])
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({
                "AAPL": {"prev_close": 100.0, "open": 100.0, "last": 110.0},
                "MSFT": {"prev_close": 100.0, "open": 100.0, "last": 100.0},
            }),
        )
        assert t.n_priced == 2 and t.n_excluded == 0
        assert t.covered_prev_mv == 2000.0
        assert t.day_gain == 100.0 and t.day_pct == pytest.approx(0.05)

    def test_unquoted_absent_from_numerator_and_denominator(self, db_session):
        # MSFT has no quote: the portfolio day% stays the covered position's own 10% —
        # never diluted toward zero by a coverage gap — and MSFT is named with a reason.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
            {"symbol": "MSFT", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
        ])
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({"AAPL": {"prev_close": 100.0, "open": 100.0, "last": 110.0}}),
        )
        assert t.n_priced == 1 and t.n_excluded == 1
        assert t.covered_prev_mv == 1000.0
        assert t.day_pct == pytest.approx(0.10)
        assert [(e.ticker, e.reason) for e in t.excluded_rows] == [("MSFT", "no_quote")]

    def test_exclusion_reasons_suspect_and_no_fx(self, db_session):
        # SUSP carries a suspect-flagged quote; 0005.HK decomposes but has no FX to base.
        tid, pid = _seed_portfolio_multi(db_session, [
            {"symbol": "AAPL", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
            {"symbol": "SUSP", "qty": 10, "buy_px": 50.0, "eod_close": 100.0},
            {"symbol": "0005.HK", "qty": 10, "buy_px": 50.0, "eod_close": 60.0, "currency": "HKD"},
        ])
        t = intraday.today_view(
            db_session, tid, pid, feed_entitled=True, now=_NOW,
            reader=lambda: _art({
                "AAPL": {"prev_close": 100.0, "open": 100.0, "last": 110.0},
                "SUSP": {"prev_close": 100.0, "open": 100.0, "last": 250.0, "suspect": True},
                "0005.HK": {"prev_close": 60.0, "open": 60.0, "last": 66.0},
            }),
        )
        reasons = {e.ticker: e.reason for e in t.excluded_rows}
        assert reasons == {"SUSP": "suspect", "0005.HK": "no_fx"}
        assert t.n_priced == 1 and t.covered_prev_mv == 1000.0


class TestHoldingsEndpointIsEstimated:
    """``GET .../holdings`` stamps ``is_estimated`` (metron-ops#112) on a late-striking
    fund's row when its live price came from the tracking-proxy same-day estimate, and
    leaves every normally-quoted holding False — end-to-end through the router.

    Uses ``client`` + ``db_session`` together so both share the same in-memory engine
    (see conftest): the API seeds the portfolio/holdings, ``db_session`` seeds the FNILX
    EOD close bar the estimate scales, and ``now`` is real "today" (the endpoint doesn't
    take an injectable clock) so the artifact's ``session_date`` must match it."""

    def _seed_portfolio(self, client, db_session, tenant, monkeypatch):
        import io

        from api.services import indices as indices_service
        from api.services import security_perf

        monkeypatch.setattr(settings, "feed_entitled", True)
        # Reset the process-level snapshot TTL cache so each test's reader is honored.
        monkeypatch.setattr(intraday, "_snapshot_cache", None, raising=False)
        monkeypatch.setattr(intraday, "_snapshot_fetched_monotonic", 0.0, raising=False)

        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        client.put(
            f"/portfolios/{pid}/preferences",
            json={"intraday_enabled": True}, headers={"X-Tenant-Id": tenant},
        )
        csv = (
            "date,type,symbol,quantity,price,amount,account\n"
            "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
            "2024-01-02,BUY,FNILX,10,20,200,Brokerage\n"
        )
        r = client.post(
            f"/portfolios/{pid}/import/csv",
            files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200

        # Seed FNILX's own cached EOD close directly (the CSV import doesn't fetch prices) —
        # the estimate scales THIS close by the proxy's same-day return.
        from api.db import models

        sec = db_session.scalar(select(models.Security).where(models.Security.symbol == "FNILX"))
        today = security_perf.market_today()
        db_session.add(models.PriceBar(security_id=sec.id, bar_date=today, close=20.0, currency="USD"))
        db_session.commit()

        session_today = today.isoformat()
        art = {
            "schema_version": 2,
            "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "quotes": {"AAPL": {"last": 130.0, "session_date": session_today}},
            "fund_proxies": {"SPY": {"last": 102.0, "prev_close": 100.0, "session_date": session_today}},
        }
        monkeypatch.setattr(intraday, "_default_reader", lambda: art)
        monkeypatch.setattr(indices_service, "_default_reader", lambda: art)
        return pid

    def test_synthesized_fund_row_flagged_estimated(self, client, db_session, tenant, monkeypatch):
        # ?valuation=live — the overlay (and thus the estimate) is live-mode-only now
        # (metron-ops#153); the settled default never synthesizes.
        pid = self._seed_portfolio(client, db_session, tenant, monkeypatch)
        rows = client.get(f"/portfolios/{pid}/holdings?valuation=live", headers={"X-Tenant-Id": tenant}).json()
        by_ticker = {r["ticker"]: r for r in rows}
        assert by_ticker["FNILX"]["is_estimated"] is True
        assert by_ticker["FNILX"]["last_price"] == pytest.approx(20.0 * 1.02)

    def test_normally_quoted_holding_not_flagged_estimated(self, client, db_session, tenant, monkeypatch):
        pid = self._seed_portfolio(client, db_session, tenant, monkeypatch)
        rows = client.get(f"/portfolios/{pid}/holdings?valuation=live", headers={"X-Tenant-Id": tenant}).json()
        by_ticker = {r["ticker"]: r for r in rows}
        assert by_ticker["AAPL"]["is_estimated"] is False

    def test_settled_default_never_serves_live_values(self, client, db_session, tenant, monkeypatch):
        """The regime contract (metron-ops#153/#154): a holdings read that doesn't ask for
        live gets the official close — no overlay, no estimate, no session day legs — even
        with the feed on, the toggle on, and a fresh snapshot available."""
        pid = self._seed_portfolio(client, db_session, tenant, monkeypatch)
        rows = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
        by_ticker = {r["ticker"]: r for r in rows}
        assert by_ticker["AAPL"]["last_price"] is None or by_ticker["AAPL"]["last_price"] != 130.0
        assert by_ticker["FNILX"]["is_estimated"] is False
        assert by_ticker["FNILX"]["last_price"] == pytest.approx(20.0)  # its own EOD close
        assert all(r["day_pct"] is None for r in rows)  # session legs are live-mode-only


class TestIntradayStatusCoverageEndpoint:
    """``GET .../intraday`` carries the NAV-weighted coverage + per-ticker sources
    (metron-ops#152) end-to-end through the router when the overlay is applied."""

    def test_applied_reports_nav_weighted_coverage(self, client, db_session, tenant, monkeypatch):
        import io

        monkeypatch.setattr(settings, "feed_entitled", True)
        monkeypatch.setattr(intraday, "_snapshot_cache", None, raising=False)
        monkeypatch.setattr(intraday, "_snapshot_fetched_monotonic", 0.0, raising=False)

        pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
        client.put(
            f"/portfolios/{pid}/preferences",
            json={"intraday_enabled": True}, headers={"X-Tenant-Id": tenant},
        )
        csv = (
            "date,type,symbol,quantity,price,amount,account\n"
            "2024-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
            "2024-01-02,BUY,MSFT,10,100,1000,Brokerage\n"
        )
        r = client.post(
            f"/portfolios/{pid}/import/csv",
            files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200
        for sym, close in (("AAPL", 120.0), ("MSFT", 100.0)):
            sec = db_session.scalar(select(models.Security).where(models.Security.symbol == sym))
            db_session.add(models.PriceBar(security_id=sec.id, bar_date=date(2026, 6, 11), close=close, currency="USD"))
        db_session.commit()

        art = {
            "schema_version": 2,
            "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "quotes": {"AAPL": {"last": 130.0}},
        }
        monkeypatch.setattr(intraday, "_default_reader", lambda: art)

        body = client.get(f"/portfolios/{pid}/intraday", headers={"X-Tenant-Id": tenant}).json()
        assert body["applied"] is True
        assert body["n_priced"] == 1 and body["n_total"] == 2
        # NAV-weighted, in live-NAV terms: AAPL 10 × 130 covered; MSFT 10 × 100 at last close.
        assert body["covered_nav"] == 1300.0 and body["total_nav"] == 2300.0
        assert body["sources"] == {"AAPL": "delayed", "MSFT": "last_close"}


class TestSessionState:
    """``session_state`` (metron-ops-I156): the valuation toggle's honest label — "live"
    in session, "recap" post-close same trading day, "closed" pre-market/weekend/holiday
    (and as the conservative fallback for a mid-session feed outage)."""

    def _meta(self, *, applied=False, as_of=None):
        return intraday.IntradayMeta(applied=applied, as_of_utc=as_of, stale=not applied)

    def test_applied_is_live(self):
        m = self._meta(applied=True, as_of="2026-07-07T17:00:00Z")
        assert intraday.session_state(m, now=datetime(2026, 7, 7, 17, 3, tzinfo=UTC)) == "live"

    def test_post_close_same_day_is_recap(self):
        # Tue 2026-07-07 21:30 UTC (5:30 PM ET, session closed); snapshot written 20:05 UTC
        # the same session → the completed session's closing state.
        m = self._meta(as_of="2026-07-07T20:05:35Z")
        assert intraday.session_state(m, now=datetime(2026, 7, 7, 21, 30, tzinfo=UTC)) == "recap"

    def test_pre_market_next_day_is_closed(self):
        # Mon 2026-07-06 12:00 UTC (8 AM ET pre-market after the 7/3 holiday weekend);
        # the snapshot is THU 7/2's close — a prior session, nothing live can add.
        m = self._meta(as_of="2026-07-02T20:05:00Z")
        assert intraday.session_state(m, now=datetime(2026, 7, 6, 12, 0, tzinfo=UTC)) == "closed"

    def test_weekend_is_closed(self):
        m = self._meta(as_of="2026-07-10T20:05:00Z")  # Friday's close, viewed Saturday
        assert intraday.session_state(m, now=datetime(2026, 7, 11, 15, 0, tzinfo=UTC)) == "closed"

    def test_mid_session_feed_outage_falls_back_closed(self):
        # Tue midday with a stale snapshot (feed died >20 min ago): today's session hasn't
        # closed, so this is NOT a recap — gray conservatively rather than fake live-ness.
        m = self._meta(as_of="2026-07-07T15:00:00Z")
        assert intraday.session_state(m, now=datetime(2026, 7, 7, 17, 0, tzinfo=UTC)) == "closed"

    def test_no_snapshot_is_closed(self):
        assert intraday.session_state(self._meta(), now=datetime(2026, 7, 7, 21, 30, tzinfo=UTC)) == "closed"
