"""Major-index intraday strip — the Overview "markets" row (SPY/QQQ/IWM proxies).

Service: reads the data-spine intraday artifact's ``indices`` map, computes change /
change% vs prior close, maps each ETF to its index label, flags staleness, and reports
unavailable WITH a reason (never fabricated) when the artifact / its indices are absent.
Endpoint: feed-gated (Pro) — locked WITH the upsell tier in the no-feed beta, honoring
the owner tier-simulator preview header.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from api.config import settings
from api.db import models
from api.services import indices, security_perf
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint

_AS_OF = "2026-06-12T15:00:00Z"
_NOW = datetime(2026, 6, 12, 15, 3, tzinfo=UTC)  # 3 min after the write — fresh
_ART = {
    "schema_version": 2,
    "as_of_utc": _AS_OF,
    "source": "yfinance_delayed",
    "quotes": {"AAPL": {"last": 202.1, "prev_close": 201.5}},
    "indices": {
        "SPY": {"last": 605.2, "open": 603.0, "prev_close": 602.4, "session_date": "2026-06-12"},
        "ONEQ": {"last": 101.4, "open": 100.8, "prev_close": 100.5, "session_date": "2026-06-12"},
        "QQQ": {"last": 540.1, "open": 538.5, "prev_close": 537.0, "session_date": "2026-06-12"},
        "IWM": {"last": 215.3, "open": 216.0, "prev_close": 216.5, "session_date": "2026-06-12"},
    },
}


class TestLoadIndices:
    def test_builds_quotes_with_change_labels_and_order(self):
        snap = indices.load_indices(reader=lambda: _ART, now=_NOW)
        assert snap.available is True and snap.stale is False
        assert snap.as_of_utc == _AS_OF
        assert [q.symbol for q in snap.indices] == ["SPY", "ONEQ", "QQQ", "IWM"]  # display order
        spy = snap.indices[0]
        assert spy.label == "S&P 500"
        assert spy.change == pytest.approx(605.2 - 602.4)
        assert spy.change_pct == pytest.approx((605.2 - 602.4) / 602.4)
        # Both Nasdaq proxies are shown with distinct labels (the Composite/100 divergence).
        assert snap.indices[1].label == "Nasdaq Composite" and snap.indices[1].symbol == "ONEQ"
        assert snap.indices[2].label == "Nasdaq 100" and snap.indices[2].symbol == "QQQ"
        # A down index keeps the sign — never abs/clamped.
        iwm = snap.indices[3]
        assert iwm.label == "Russell 2000" and iwm.change == pytest.approx(215.3 - 216.5)
        assert iwm.change_pct < 0

    def test_unavailable_when_no_artifact(self):
        snap = indices.load_indices(reader=lambda: None, now=_NOW)
        assert snap.available is False and snap.reason

    def test_unavailable_when_indices_absent_but_as_of_preserved(self):
        snap = indices.load_indices(reader=lambda: {"as_of_utc": _AS_OF, "indices": {}}, now=_NOW)
        assert snap.available is False and snap.reason
        assert snap.as_of_utc == _AS_OF

    def test_absent_symbol_omitted_not_fabricated(self):
        art = {"as_of_utc": _AS_OF, "indices": {"SPY": _ART["indices"]["SPY"]}}
        snap = indices.load_indices(reader=lambda: art, now=_NOW)
        assert [q.symbol for q in snap.indices] == ["SPY"]

    def test_missing_prev_close_yields_none_change(self):
        art = {"as_of_utc": _AS_OF, "indices": {"SPY": {"last": 605.2}}}
        snap = indices.load_indices(reader=lambda: art, now=_NOW)
        q = snap.indices[0]
        assert q.last == 605.2 and q.change is None and q.change_pct is None

    def test_stale_when_snapshot_old(self):
        old_now = datetime(2026, 6, 12, 16, 0, tzinfo=UTC)  # ~1h after the write
        snap = indices.load_indices(reader=lambda: _ART, now=old_now)
        assert snap.available is True and snap.stale is True

    def test_suspect_flag_passthrough(self):
        art = {"as_of_utc": _AS_OF, "indices": {"SPY": {"last": 9.9, "prev_close": 602.4, "suspect": True}}}
        snap = indices.load_indices(reader=lambda: art, now=_NOW)
        assert snap.indices[0].suspect is True

    def test_change_shown_when_session_is_today(self):
        """The artifact is dated for the current NYSE session → a real TODAY move shows."""
        # _NOW is 2026-06-12 15:03Z = 11:03 ET, so the market session is 2026-06-12, matching
        # every fixture quote's session_date.
        assert security_perf.market_today(_NOW).isoformat() == "2026-06-12"
        snap = indices.load_indices(reader=lambda: _ART, now=_NOW)
        spy = snap.indices[0]
        assert spy.last == pytest.approx(605.2)  # level still rendered
        assert spy.change == pytest.approx(605.2 - 602.4)
        assert spy.change_pct == pytest.approx((605.2 - 602.4) / 602.4)

    def test_stale_prior_session_move_suppressed_preopen(self):
        """metron-ops#96: pre-open the overnight artifact still carries the PRIOR session's
        last/prev_close. With ``now`` on a LATER session than the quote's ``session_date``,
        the strip must show the level but NO "today" move (else yesterday's move reads as
        TODAY). The level + session_date are preserved; only change/change_pct go flat."""
        # now = the next session's pre-open (2026-06-15 08:00 ET = 12:00Z); the artifact is
        # still last night's, dated 2026-06-12.
        preopen = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        assert security_perf.market_today(preopen).isoformat() == "2026-06-15"  # != 2026-06-12
        snap = indices.load_indices(reader=lambda: _ART, now=preopen)
        for q in snap.indices:
            assert q.last is not None, f"{q.symbol} level dropped"  # level still shown
            assert q.session_date == "2026-06-12"
            assert q.change is None, f"{q.symbol} stale change leaked as TODAY"
            assert q.change_pct is None, f"{q.symbol} stale change_pct leaked as TODAY"

    def test_missing_session_date_suppresses_move(self):
        """A quote with no ``session_date`` can't be proven to be the current session, so its
        move is suppressed (fail-safe — never assume an undated quote is today's)."""
        art = {"as_of_utc": _AS_OF, "indices": {"SPY": {"last": 605.2, "prev_close": 602.4}}}
        snap = indices.load_indices(reader=lambda: art, now=_NOW)
        q = snap.indices[0]
        assert q.last == pytest.approx(605.2) and q.change is None and q.change_pct is None


class TestIndicesEndpoint:
    def test_available_returns_indices_on_feed_deployment(self, client, monkeypatch):
        # Default settings: personal tier + feed entitled → indices available.
        monkeypatch.setattr(indices, "_default_reader", lambda: _ART)
        # The YTD/LTM enrichment must not reach the network in a unit test.
        monkeypatch.setattr("api.services.prices.fetch_close_history", lambda *a, **k: {})
        body = client.get("/indices/intraday").json()
        assert body["available"] is True and body["required_tier"] is None
        assert [q["symbol"] for q in body["indices"]] == ["SPY", "ONEQ", "QQQ", "IWM"]
        assert body["indices"][0]["label"] == "S&P 500"
        assert body["indices"][1]["label"] == "Nasdaq Composite"

    def test_locked_when_feed_not_entitled(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(indices, "_default_reader", lambda: called.append(1) or _ART)
        monkeypatch.setattr(settings, "feed_entitled", False)  # the no-feed beta
        body = client.get("/indices/intraday").json()
        assert body["available"] is False
        assert body["reason"] == "feed" and body["required_tier"] == "personal"
        assert not called  # locked → never reads the (licensed) data

    def test_simulator_preview_feed_off_locks(self, client, monkeypatch):
        monkeypatch.setattr(indices, "_default_reader", lambda: _ART)
        monkeypatch.setattr(settings, "tier_simulator", True)
        body = client.get("/indices/intraday", headers={"X-Preview-Feed": "false"}).json()
        assert body["available"] is False and body["required_tier"] == "personal"

    def test_entitled_but_no_data_is_unavailable_without_required_tier(self, client, monkeypatch):
        monkeypatch.setattr(indices, "_default_reader", lambda: None)
        body = client.get("/indices/intraday").json()
        assert body["available"] is False and body["required_tier"] is None and body["reason"]

    def test_endpoint_populates_ytd_ltm_for_all_indices_without_a_perf_visit(self, client, monkeypatch):
        """The strip owns its own close-history coverage: YTD/LTM populate for EVERY proxy
        on a cold DB (no prior Performance-page backfill), not just SPY."""
        monkeypatch.setattr(indices, "_default_reader", lambda: _ART)
        # Inject a deterministic history that reaches back past both window starts: a
        # prior-year bar (so the LTM window resolves), a Jan-of-this-year bar (the YTD
        # reference), and a latest bar. The coverage backfill resolves from it, no network.
        today = date.today()
        hist = {
            sym: [
                ClosePoint(date(today.year - 1, 1, 1), 100.0),
                ClosePoint(date(today.year, 1, 2), 100.0),
                ClosePoint(today, level),
            ]
            for sym, level in (("SPY", 110.0), ("ONEQ", 120.0), ("QQQ", 130.0), ("IWM", 90.0))
        }
        monkeypatch.setattr(
            "api.services.prices.fetch_close_history",
            lambda targets, start, end, *, source=None: {s: hist[s] for s in targets if s in hist},
        )
        body = client.get("/indices/intraday").json()
        assert body["available"] is True
        for q in body["indices"]:
            assert q["ytd_pct"] is not None, f"{q['symbol']} missing YTD"
            assert q["ltm_pct"] is not None, f"{q['symbol']} missing LTM"
        oneq = next(q for q in body["indices"] if q["symbol"] == "ONEQ")
        assert oneq["ytd_pct"] == pytest.approx(120.0 / 100.0 - 1.0)  # 120 vs the Jan-of-year 100


class TestProxyDayReturn:
    """``fund_proxies`` map (metron-ops#112): the input to the late-striking-fund same-day
    ESTIMATE (mechanism B, api/services/fund_proxy.py). Mirrors the ``indices`` map's "only
    a quote dated for the live session carries a real today move" discipline exactly."""

    _ART = {
        "as_of_utc": _AS_OF,
        "fund_proxies": {
            "SPY": {"last": 605.2, "prev_close": 602.4, "session_date": "2026-06-12"},
            "IXUS": {"last": 68.0, "prev_close": 68.68, "session_date": "2026-06-12"},
        },
    }

    def test_same_session_returns_fractional_move(self):
        r = indices.proxy_day_return("SPY", reader=lambda: self._ART, now=_NOW)
        assert r == pytest.approx((605.2 - 602.4) / 602.4)

    def test_negative_move_keeps_sign(self):
        r = indices.proxy_day_return("IXUS", reader=lambda: self._ART, now=_NOW)
        assert r == pytest.approx((68.0 - 68.68) / 68.68)
        assert r < 0

    def test_missing_symbol_returns_none(self):
        assert indices.proxy_day_return("QQQ", reader=lambda: self._ART, now=_NOW) is None

    def test_missing_artifact_returns_none(self):
        assert indices.proxy_day_return("SPY", reader=lambda: None, now=_NOW) is None

    def test_wrong_session_returns_none(self):
        """Pre-open the overnight artifact still carries the PRIOR session's quote — no
        "today" move to lend the fund, so this must be None (never leak yesterday's move
        in as today's estimate)."""
        preopen = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)  # next session's pre-open
        assert indices.proxy_day_return("SPY", reader=lambda: self._ART, now=preopen) is None

    def test_missing_session_date_returns_none(self):
        art = {"as_of_utc": _AS_OF, "fund_proxies": {"SPY": {"last": 605.2, "prev_close": 602.4}}}
        assert indices.proxy_day_return("SPY", reader=lambda: art, now=_NOW) is None

    def test_missing_prev_close_returns_none(self):
        art = {
            "as_of_utc": _AS_OF,
            "fund_proxies": {"SPY": {"last": 605.2, "session_date": "2026-06-12"}},
        }
        assert indices.proxy_day_return("SPY", reader=lambda: art, now=_NOW) is None

    def test_zero_prev_close_returns_none(self):
        art = {
            "as_of_utc": _AS_OF,
            "fund_proxies": {"SPY": {"last": 605.2, "prev_close": 0, "session_date": "2026-06-12"}},
        }
        assert indices.proxy_day_return("SPY", reader=lambda: art, now=_NOW) is None

    def test_absent_fund_proxies_map_returns_none(self):
        art = {"as_of_utc": _AS_OF, "indices": {}}
        assert indices.proxy_day_return("SPY", reader=lambda: art, now=_NOW) is None


class TestEnsureIndexHistory:
    def test_backfills_only_uncovered_proxies(self, db_session, monkeypatch):
        today = date(2026, 6, 25)
        # SPY already fully covered in the cache → must NOT be refetched.
        sid = price_service.ensure_security(db_session, "SPY")
        for when, close in [(date(2025, 1, 1), 400.0), (date(2026, 1, 2), 400.0), (today, 480.0)]:
            db_session.add(models.PriceBar(security_id=sid, bar_date=when, close=close, currency="USD"))
        db_session.commit()

        fetched: list[str] = []

        def _fake_fetch(targets, start, end, *, source=None):
            fetched.extend(targets)
            return {
                s: [ClosePoint(date(2025, 1, 1), 100.0), ClosePoint(date(2026, 1, 2), 100.0), ClosePoint(today, 120.0)]
                for s in targets
            }

        monkeypatch.setattr("api.services.prices.fetch_close_history", _fake_fetch)
        security_perf.ensure_index_history(db_session, ["SPY", "ONEQ", "QQQ", "IWM"], as_of=today)

        # SPY skipped (already covered); the three uncovered proxies fetched once each.
        assert "SPY" not in fetched
        assert set(fetched) == {"ONEQ", "QQQ", "IWM"}
        periods = security_perf.index_period_returns(db_session, ["SPY", "ONEQ"], as_of=today)
        assert periods["SPY"][0] == pytest.approx(480.0 / 400.0 - 1.0)  # uses the seeded SPY bars
        assert periods["ONEQ"][0] == pytest.approx(120.0 / 100.0 - 1.0)  # uses the backfilled bars
