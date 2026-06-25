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

from api.config import settings
from api.db import models
from api.services import analytics, intraday

_AS_OF = "2026-06-12T15:00:00Z"
_NOW = datetime(2026, 6, 12, 15, 3, tzinfo=UTC)   # 3 min after the write — fresh
_STALE_NOW = datetime(2026, 6, 12, 15, 45, tzinfo=UTC)  # 45 min after — stale


def _art(quotes: dict) -> dict:
    return {"schema_version": 1, "as_of_utc": _AS_OF, "source": "yfinance_delayed", "quotes": quotes}


def _seed_one_holding(session, *, symbol="AAPL", yf_symbol="AAPL", qty=10, buy_px=100.0, eod_close=120.0):
    """One USD holding with a BUY (cost basis) + a cached EOD close bar."""
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.flush()
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


class TestSnapshotCache:
    """The default reader collapses a page's 5–7 snapshot reads into one S3 GetObject per TTL
    (the Holdings-page latency fix). Freshness is still judged per-call in load_quotes, so a
    cached-but-aged artifact still reports stale — covered by TestLoadQuotes."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        # Fully reset the module cache + a controllable monotonic clock for each test.
        self.clock = [1000.0]
        monkeypatch.setattr(intraday.time, "monotonic", lambda: self.clock[0])
        monkeypatch.setattr(intraday, "_snapshot_cache", None, raising=False)
        monkeypatch.setattr(intraday, "_snapshot_fetched_monotonic", 0.0, raising=False)

    def test_reads_once_within_ttl(self, monkeypatch):
        calls = {"n": 0}

        def _reader():
            calls["n"] += 1
            return _art({"AAPL": {"last": 130.0}})

        monkeypatch.setattr(intraday, "_read_snapshot_s3", _reader)
        first = intraday._default_reader()
        for _ in range(5):
            assert intraday._default_reader() == first
        assert calls["n"] == 1  # one S3 read serves the whole fan-out

    def test_refetches_after_ttl(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(intraday, "_read_snapshot_s3", lambda: calls.__setitem__("n", calls["n"] + 1) or _art({}))
        intraday._default_reader()
        self.clock[0] += intraday._SNAPSHOT_TTL_S + 1  # advance past the window
        intraday._default_reader()
        assert calls["n"] == 2

    def test_failed_read_cached_for_window(self, monkeypatch):
        """A transient S3 failure within a page load isn't retried 5–7×; it degrades to EOD
        (None) for the window, then recovers after the TTL."""
        calls = {"n": 0}
        monkeypatch.setattr(intraday, "_read_snapshot_s3", lambda: calls.__setitem__("n", calls["n"] + 1) or None)
        assert intraday._default_reader() is None
        assert intraday._default_reader() is None
        assert calls["n"] == 1


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
        assert meta.applied is True and meta.n_priced == 1
        assert prices["AAPL"].close == 130.0  # intraday last, not the 120 EOD close
        assert prices["AAPL"].bar_date == date(2026, 6, 12)

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
        r = client.get(f"/portfolios/{pid}/intraday", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200
        assert r.json()["applied"] is False
