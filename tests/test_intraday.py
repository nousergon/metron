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
        # Coverage fields (metron-ops#146) are additive with safe defaults.
        assert body["n_total"] == 0 and body["n_estimated"] == 0


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
        pid = self._seed_portfolio(client, db_session, tenant, monkeypatch)
        rows = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
        by_ticker = {r["ticker"]: r for r in rows}
        assert by_ticker["FNILX"]["is_estimated"] is True
        assert by_ticker["FNILX"]["last_price"] == pytest.approx(20.0 * 1.02)

    def test_normally_quoted_holding_not_flagged_estimated(self, client, db_session, tenant, monkeypatch):
        pid = self._seed_portfolio(client, db_session, tenant, monkeypatch)
        rows = client.get(f"/portfolios/{pid}/holdings", headers={"X-Tenant-Id": tenant}).json()
        by_ticker = {r["ticker"]: r for r in rows}
        assert by_ticker["AAPL"]["is_estimated"] is False
