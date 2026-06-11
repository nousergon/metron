"""Data-spine publisher: the held-ticker universe Metron writes to S3 for
`alpha-engine-data` to consume (which EOD closes + FX pairs to pull).

`alpha-engine-data` is the system's sole market-data producer; this is Metron's
publish side of that contract. Covers: the payload built from multi-portfolio holdings
(deduped, foreign listings under their yf_symbol, distinct non-USD currencies), the S3
write at the pinned key, and the daily-refresh enable-gate (OFF → never touches S3).
"""

from __future__ import annotations

import json
from datetime import date

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
        # No price source → prices skipped, but the publish gate is what we assert.
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert called["n"] == 0
        assert result.universe_published is False

    def test_refresh_publishes_when_enabled(self, db_session, monkeypatch):
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        monkeypatch.setattr(maintenance.settings, "market_data_sync_enabled", True)
        called = {"n": 0}
        monkeypatch.setattr(
            data_spine, "publish_holdings_universe",
            lambda *a, **k: called.__setitem__("n", called["n"] + 1),
        )
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert called["n"] == 1
        assert result.universe_published is True

    def test_publish_failure_is_non_fatal(self, db_session, monkeypatch):
        """A data-spine S3 failure must WARN and let daily-refresh complete."""
        _seed_holding(db_session, pf_name="P1", symbol="AAPL", currency="USD", yf_symbol="AAPL")
        monkeypatch.setattr(maintenance.settings, "market_data_sync_enabled", True)

        def _boom(*a, **k):
            raise data_spine.DataSpineUnavailable("s3 down")

        monkeypatch.setattr(data_spine, "publish_holdings_universe", _boom)
        result = maintenance.daily_refresh(db_session, today=date(2024, 6, 3))
        assert result.universe_published is False
        assert result.portfolios == 1  # the rest of the refresh still completed
