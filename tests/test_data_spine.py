"""Data-spine publisher: the held-ticker universe Metron writes to S3 for
`alpha-engine-data` to consume (which EOD closes + FX pairs to pull).

`alpha-engine-data` is the system's sole market-data producer; this is Metron's
publish side of that contract. Covers: the payload built from multi-portfolio holdings
(deduped, foreign listings under their yf_symbol, distinct non-USD currencies), the S3
write at the pinned key, and the daily-refresh enable-gate (OFF → never touches S3).
"""

from __future__ import annotations

import json
from datetime import UTC, date

from api import maintenance
from api.db import models
from api.services import data_spine


class _FakeS3:
    """Captures put_object calls so the publish can be asserted without real S3."""

    def __init__(self):
        self.puts: list[dict] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None):  # noqa: N803 - boto3 kwarg names
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType})


def _seed_holding(session, *, pf_name, symbol, currency, yf_symbol, qty=10, price=100.0, external_id="A1"):
    tenant = models.Tenant(name=pf_name)
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name=pf_name, base_currency="USD")
    session.add(pf)
    session.flush()
    acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade",
        external_id=external_id, currency="USD",
    )
    session.add(acct)
    # Securities are GLOBAL (unique on symbol+currency) — reuse if a prior portfolio
    # already created this one, mirroring real ingestion.
    sec = session.query(models.Security).filter_by(symbol=symbol, currency=currency).first()
    if sec is None:
        sec = models.Security(symbol=symbol, currency=currency, yf_symbol=yf_symbol)
        session.add(sec)
    session.flush()
    session.add(
        models.Position(
            tenant_id=tenant.id, account_id=acct.id, security_id=sec.id,
            quantity=qty, avg_cost=price, currency=currency,
            market_price=price, market_value_local=qty * price, as_of=date(2024, 6, 3),
        )
    )
    session.commit()
    return pf


class TestBuildUniverse:
    def test_dedupes_and_resolves_yf_symbol_and_currencies(self, db_session):
        # Two portfolios; AAPL held in both (must appear once), plus a foreign listing.
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        _seed_holding(db_session, pf_name="P2", symbol="AAPL", currency="USD", yf_symbol="AAPL", external_id="A2")
        _seed_holding(db_session, pf_name="P2b", symbol="1299", currency="HKD", yf_symbol="1299.HK", external_id="A3")

        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))

        assert payload["schema_version"] == data_spine.HOLDINGS_UNIVERSE_SCHEMA_VERSION
        assert payload["as_of"] == "2024-06-03"
        assert payload["source"] == "metron"
        holdings = {h["yf_symbol"]: h["currency"] for h in payload["holdings"]}
        # AAPL once (deduped across portfolios), foreign listing under its yf_symbol.
        assert holdings == {"AAPL": "USD", "1299.HK": "HKD"}
        # Only non-USD currencies listed for the FX producer.
        assert payload["currencies"] == ["HKD"]

    def test_empty_when_no_holdings(self, db_session):
        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))
        assert payload["holdings"] == []
        assert payload["currencies"] == []
        assert payload["tickers"] == []

    def test_tickers_are_symbols_only_deduped_sorted_broker_symbol(self, db_session):
        """The ``tickers`` slice is the nousergon-data daily-news union's source
        (config#1506): a symbols-only, deduped, sorted list of the BROKER symbols
        (not the exchange-suffixed yf_symbol) — the exact ``{"tickers": [...]}``
        shape the retired robodashboard producer fed the union."""
        # AAPL held in two portfolios (must appear once), plus a foreign listing whose
        # broker symbol (1299) differs from its yf_symbol (1299.HK).
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        _seed_holding(db_session, pf_name="P2", symbol="AAPL", currency="USD", yf_symbol="AAPL", external_id="A2")
        _seed_holding(db_session, pf_name="P3", symbol="1299", currency="HKD", yf_symbol="1299.HK", external_id="A3")

        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))

        # Symbols-only list of the BROKER ticker (1299, not 1299.HK), deduped + sorted.
        assert payload["tickers"] == ["1299", "AAPL"]
        # Every entry is a bare string (the consumer does str(t).upper()).
        assert all(isinstance(t, str) for t in payload["tickers"])
        # tickers ⊆ the same held set as holdings — two views, never drifting.
        assert {"AAPL"} <= {h["yf_symbol"] for h in payload["holdings"]}

    def test_tickers_exclude_unlisted(self, db_session):
        """An unlisted broker-priced instrument (no public listing → no news feed)
        stays out of the news-universe ``tickers`` just as it does the yf holdings."""
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        _seed_holding(db_session, pf_name="P1b", symbol="PCKM", currency="USD",
                      yf_symbol="PCKM", external_id="A9")
        sec = db_session.query(models.Security).filter_by(symbol="PCKM").one()
        sec.yf_unlisted = True
        db_session.commit()

        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))

        assert payload["tickers"] == ["AAPL"]


def _seed_watchlist(session, *, pf_name, symbol, currency="USD", yf_symbol=None):
    """A watchlist item (position-optional, no Position/Account row) plus the cached
    Security row `build_watchlist_universe` resolves it through — mirroring what
    `price_service.ensure_security` writes on a real watchlist add."""
    tenant = models.Tenant(name=pf_name)
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name=pf_name, base_currency="USD")
    session.add(pf)
    session.flush()
    sec = session.query(models.Security).filter_by(symbol=symbol, currency=currency).first()
    if sec is None:
        sec = models.Security(symbol=symbol, currency=currency, yf_symbol=yf_symbol or symbol)
        session.add(sec)
        session.flush()
    session.add(models.WatchlistItem(tenant_id=tenant.id, portfolio_id=pf.id, symbol=symbol))
    session.commit()
    return pf


class TestBuildWatchlistUniverse:
    """metron-ops#132: a watchlist-only ticker (never held) must still be published so
    `alpha-engine-data` fetches its fundamentals/technicals/analyst/sentiment."""

    def test_dedupes_across_portfolios_and_resolves_yf_symbol(self, db_session):
        _seed_watchlist(db_session, pf_name="P1", symbol="MU", currency="USD", yf_symbol="MU")
        _seed_watchlist(db_session, pf_name="P2", symbol="MU", currency="USD", yf_symbol="MU")

        payload = data_spine.build_watchlist_universe(db_session, today=date(2024, 6, 3))

        assert payload["schema_version"] == data_spine.WATCHLIST_UNIVERSE_SCHEMA_VERSION
        assert payload["as_of"] == "2024-06-03"
        assert payload["source"] == "metron"
        assert payload["holdings"] == [{"yf_symbol": "MU", "currency": "USD"}]
        assert payload["tickers"] == ["MU"]

    def test_empty_when_no_watchlist_items(self, db_session):
        payload = data_spine.build_watchlist_universe(db_session, today=date(2024, 6, 3))
        assert payload["holdings"] == [] and payload["currencies"] == [] and payload["tickers"] == []

    def test_symbol_with_no_cached_security_yet_is_skipped_not_fabricated(self, db_session):
        # A watchlist add that hasn't resolved a Security row yet (e.g. a raced first
        # request) never fabricates a yf_symbol/currency — it's just absent this cycle.
        tenant = models.Tenant(name="P1")
        db_session.add(tenant)
        db_session.flush()
        pf = models.Portfolio(tenant_id=tenant.id, name="P1", base_currency="USD")
        db_session.add(pf)
        db_session.flush()
        db_session.add(models.WatchlistItem(tenant_id=tenant.id, portfolio_id=pf.id, symbol="ZZZZ"))
        db_session.commit()

        payload = data_spine.build_watchlist_universe(db_session, today=date(2024, 6, 3))
        assert payload["holdings"] == []

    def test_excludes_unlisted(self, db_session):
        _seed_watchlist(db_session, pf_name="P1", symbol="PCKM", currency="USD", yf_symbol="PCKM")
        sec = db_session.query(models.Security).filter_by(symbol="PCKM").one()
        sec.yf_unlisted = True
        db_session.commit()

        payload = data_spine.build_watchlist_universe(db_session, today=date(2024, 6, 3))
        assert payload["holdings"] == []


class TestPublish:
    def test_publish_writes_compact_json_at_pinned_key(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="NVDA", currency="USD", yf_symbol="NVDA")
        fake = _FakeS3()

        payload = data_spine.publish_holdings_universe(
            db_session, s3_client=fake, today=date(2024, 6, 3), bucket="test-bucket"
        )

        assert len(fake.puts) == 1
        put = fake.puts[0]
        assert put["Bucket"] == "test-bucket"
        assert put["Key"] == data_spine.HOLDINGS_UNIVERSE_KEY == "metron/holdings_universe.json"
        assert put["ContentType"] == "application/json"
        # Body round-trips to the returned payload.
        assert json.loads(put["Body"].decode()) == payload
        assert payload["holdings"] == [{"yf_symbol": "NVDA", "currency": "USD"}]

    def test_publish_watchlist_writes_compact_json_at_pinned_key(self, db_session):
        _seed_watchlist(db_session, pf_name="P1", symbol="MU", currency="USD", yf_symbol="MU")
        fake = _FakeS3()

        payload = data_spine.publish_watchlist_universe(
            db_session, s3_client=fake, today=date(2024, 6, 3), bucket="test-bucket"
        )

        assert len(fake.puts) == 1
        put = fake.puts[0]
        assert put["Bucket"] == "test-bucket"
        assert put["Key"] == data_spine.WATCHLIST_UNIVERSE_KEY == "metron/watchlist_universe.json"
        assert put["ContentType"] == "application/json"
        assert json.loads(put["Body"].decode()) == payload
        assert payload["holdings"] == [{"yf_symbol": "MU", "currency": "USD"}]


class TestDailyRefreshGate:
    def test_refresh_does_not_publish_when_disabled(self, db_session, monkeypatch):
        """Default (flag off): daily-refresh must never reach the data spine."""
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        monkeypatch.setattr(maintenance.settings, "market_data_sync_enabled", False)
        called = {"n": 0}
        monkeypatch.setattr(
            data_spine, "publish_holdings_universe",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        watchlist_called = {"n": 0}
        monkeypatch.setattr(
            data_spine, "publish_watchlist_universe",
            lambda *a, **k: watchlist_called.__setitem__("n", watchlist_called["n"] + 1),
        )
        # No price source → prices skipped, but the publish gate is what we assert.
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert called["n"] == 0 and watchlist_called["n"] == 0
        assert result.universe_published is False
        assert result.watchlist_universe_published is False

    def test_refresh_publishes_when_enabled(self, db_session, monkeypatch):
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        monkeypatch.setattr(maintenance.settings, "market_data_sync_enabled", True)
        called = {"n": 0}
        monkeypatch.setattr(
            data_spine, "publish_holdings_universe",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        watchlist_called = {"n": 0}
        monkeypatch.setattr(
            data_spine, "publish_watchlist_universe",
            lambda *a, **k: watchlist_called.__setitem__("n", watchlist_called["n"] + 1),
        )
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert called["n"] == 1 and watchlist_called["n"] == 1
        assert result.universe_published is True
        assert result.watchlist_universe_published is True

    def test_publish_failure_is_non_fatal(self, db_session, monkeypatch):
        """A data-spine S3 failure must WARN and let daily-refresh complete."""
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        monkeypatch.setattr(maintenance.settings, "market_data_sync_enabled", True)

        def _boom(*a, **k):
            raise data_spine.DataSpineUnavailable("s3 down")

        monkeypatch.setattr(data_spine, "publish_holdings_universe", _boom)
        monkeypatch.setattr(data_spine, "publish_watchlist_universe", _boom)
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert result.universe_published is False
        assert result.watchlist_universe_published is False
        assert result.portfolios == 1  # the rest of the refresh still completed


class TestUiHeartbeat:
    """The intraday demand gate's producer-side signal (alpha-engine-data
    collectors/metron_market_data.py::metron_app_active reads this key)."""

    def _enable(self, monkeypatch):
        import api.services.data_spine as ds
        monkeypatch.setattr("api.services.data_spine.settings.market_data_sync_enabled", True)
        # NOT 0.0: time.monotonic() is seconds-since-boot, and a fresh CI runner VM
        # can reach this test in < _HEARTBEAT_MIN_INTERVAL_S of uptime — with 0.0 the
        # first call would be throttled and the test boot-races (flaked on metron#45
        # CI at ~57s uptime). A sentinel below -interval admits the first call for
        # ANY non-negative monotonic value.
        monkeypatch.setattr(ds, "_last_heartbeat_monotonic", -2 * ds._HEARTBEAT_MIN_INTERVAL_S)
        return ds

    def test_writes_throttles_and_reports(self, monkeypatch):
        from datetime import datetime
        from unittest.mock import MagicMock

        ds = self._enable(monkeypatch)
        s3 = MagicMock()
        now = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)
        assert ds.touch_ui_heartbeat(s3_client=s3, now=now) is True
        kw = s3.put_object.call_args.kwargs
        assert kw["Key"] == ds.UI_HEARTBEAT_KEY
        import json as _json
        body = _json.loads(kw["Body"].decode())
        assert body == {"schema_version": ds.UI_HEARTBEAT_SCHEMA_VERSION, "ts": "2026-06-12T15:00:00Z"}
        # Immediately again → throttled, no second write.
        assert ds.touch_ui_heartbeat(s3_client=s3, now=now) is False
        assert s3.put_object.call_count == 1

    def test_flag_off_is_noop(self, monkeypatch):
        from unittest.mock import MagicMock

        import api.services.data_spine as ds

        monkeypatch.setattr("api.services.data_spine.settings.market_data_sync_enabled", False)
        monkeypatch.setattr(ds, "_last_heartbeat_monotonic", 0.0)
        s3 = MagicMock()
        assert ds.touch_ui_heartbeat(s3_client=s3) is False
        assert not s3.put_object.called

    def test_s3_failure_is_fail_soft(self, monkeypatch):
        from unittest.mock import MagicMock

        ds = self._enable(monkeypatch)
        s3 = MagicMock()
        s3.put_object.side_effect = Exception("AccessDenied")
        # Never raises into the request path; reports False; next call may retry
        # (monotonic stamp only advances on success).
        before = ds._last_heartbeat_monotonic
        assert ds.touch_ui_heartbeat(s3_client=s3) is False
        assert ds._last_heartbeat_monotonic == before


class TestUnlistedExclusion:
    """yf_unlisted securities stay out of the published universe (config#1029).

    The PCKM 401(k) CIT has no public listing — publishing it made the data
    spine's yfinance pull fail 5-ways every EOD run. Broker snapshot remains
    its price authority; the universe only advertises what yfinance can price.
    """

    def test_unlisted_security_is_excluded_from_universe(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        _seed_holding(db_session, pf_name="P1b", symbol="PCKM", currency="USD",
                      yf_symbol="PCKM", external_id="A9")
        sec = db_session.query(models.Security).filter_by(symbol="PCKM").one()
        sec.yf_unlisted = True
        db_session.commit()

        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))

        assert {h["yf_symbol"] for h in payload["holdings"]} == {"AAPL"}

    def test_unflagged_security_still_published(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="PCKM", currency="USD", yf_symbol="PCKM")
        payload = data_spine.build_holdings_universe(db_session, today=date(2024, 6, 3))
        assert {h["yf_symbol"] for h in payload["holdings"]} == {"PCKM"}


class TestMarkUnlistedCommand:
    def test_marks_by_symbol_and_is_idempotent(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="PCKM", currency="USD", yf_symbol="PCKM")
        assert maintenance.mark_unlisted(db_session, "pckm") == 1
        assert maintenance.mark_unlisted(db_session, "PCKM") == 1  # re-run: same row, same result
        sec = db_session.query(models.Security).filter_by(symbol="PCKM").one()
        assert sec.yf_unlisted is True

    def test_undo_clears_flag(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="PCKM", currency="USD", yf_symbol="PCKM")
        maintenance.mark_unlisted(db_session, "PCKM")
        assert maintenance.mark_unlisted(db_session, "PCKM", unlisted=False) == 1
        sec = db_session.query(models.Security).filter_by(symbol="PCKM").one()
        assert sec.yf_unlisted is False

    def test_matches_on_yf_symbol_too(self, db_session):
        _seed_holding(db_session, pf_name="P1", symbol="1299", currency="HKD", yf_symbol="1299.HK")
        assert maintenance.mark_unlisted(db_session, "1299.HK") == 1

    def test_unknown_symbol_updates_nothing(self, db_session):
        assert maintenance.mark_unlisted(db_session, "NOPE") == 0
