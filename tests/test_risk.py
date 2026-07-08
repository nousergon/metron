"""Factor risk decomposition (C2-6c).

Synthetic, deterministic close series feed the model (never the network). Invariants:
a market-value-weighted portfolio with enough aligned history produces a positive total
vol decomposed into factor + idio with the expected factor labels; a holding with too
little history is excluded (named), not regressed on a guess; with no priced holdings
or too little market history the result is not-computable WITH a reason.
"""

from __future__ import annotations

import io
import math
import uuid
from datetime import date, timedelta

import pytest

from api.config import settings
from api.services import risk
from portfolio_analytics.prices import ClosePoint

CSV = "date,type,symbol,quantity,price\n2024-01-01,BUY,AAPL,10,100\n2024-01-01,BUY,MSFT,5,200\n"

_ETFS = ["SPY", "MTUM", "QUAL", "USMV", "VLUE", "SIZE"]


def _off(sym: str) -> int:
    return sum(ord(c) for c in sym) % 7


def _closes(sym: str, n: int = 40, start: date = date(2024, 1, 1)) -> list[ClosePoint]:
    """Deterministic, varied close series (non-degenerate returns for OLS/cov)."""
    base = 100.0 + _off(sym)
    return [
        ClosePoint(start + timedelta(days=i), round(base * (1 + 0.01 * math.sin(i + _off(sym) * 0.3) + 0.003 * math.cos(i * 0.5)), 4))
        for i in range(n)
    ]


def _full_hist(symbols, start, end, *, source=None):
    return {s: _closes(s) for s in symbols}


def _latest(symbols, *, source=None):
    # Price the held tickers so valued_holdings yields MV weights.
    return {s: ClosePoint(date(2024, 2, 9), 100.0 + _off(s)) for s in symbols if s in {"AAPL", "MSFT"}}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant, csv=CSV):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    assert client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(csv.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    ).status_code == 200
    return pid


def _refresh(client, tenant, pid, monkeypatch):
    monkeypatch.setattr("api.services.prices.fetch_latest_closes", _latest)
    monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
    client.post(f"/portfolios/{pid}/prices/refresh", headers={"X-Tenant-Id": tenant})


class TestComputeRisk:
    def test_full_decomposition(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        r = risk.compute_risk(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 2, 15), do_backfill=True, source=_full_hist
        )
        assert r.computable is True
        # as_of is the aligned grid's LAST date — the model's true data horizon (last
        # synthetic close bar = 2024-02-09), never the compute-call date (2024-02-15).
        assert r.as_of == date(2024, 2, 9)
        assert r.n_modeled == 2 and r.excluded == []
        assert set(r.factor_exposures) == set(risk.FACTORS)  # Market + 5 styles
        assert r.total_vol > 0
        assert r.factor_vol >= 0 and r.idio_vol >= 0
        assert 0.0 <= r.idio_pct <= 1.0
        assert r.tracking_error is not None and r.tracking_error >= 0

    def test_no_priced_holdings(self, client, db_session, tenant):
        pid = _seed(client, tenant)  # never refreshed → no market value → no weights
        r = risk.compute_risk(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 2, 15))
        assert r.computable is False and "priced" in r.reason.lower()

    def test_insufficient_history_without_backfill(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        # do_backfill=False and nothing cached → not computable, with a reason.
        r = risk.compute_risk(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 2, 15), do_backfill=False)
        assert r.computable is False and r.reason

    def test_short_history_holding_excluded(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)

        def src(symbols, start, end, *, source=None):
            out = {s: _closes(s) for s in symbols if s != "MSFT"}
            out["MSFT"] = _closes("MSFT", n=4)  # < _MIN_OBS returns → excluded
            return out

        r = risk.compute_risk(
            db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2024, 2, 15), do_backfill=True, source=src
        )
        assert r.computable is True
        assert "MSFT" in r.excluded and r.n_modeled == 1


class TestRiskEndpoints:
    def test_compute_then_get(self, client, tenant, monkeypatch):
        # Risk is feed-dependent; the endpoint enforces the entitlement matrix, so this
        # models a feed-entitled deployment (feed_entitled — the entitlement axis,
        # decoupled from the S3 market_data_sync_enabled infra toggle per metron-ops#43).
        # Without it the API returns computable=false (reason "feed") regardless of cached
        # history — see test_entitlements_enforcement.py.
        monkeypatch.setattr(settings, "feed_entitled", True)
        pid = _seed(client, tenant)
        _refresh(client, tenant, pid, monkeypatch)
        monkeypatch.setattr("api.services.prices.fetch_close_history", _full_hist)
        posted = client.post(f"/portfolios/{pid}/risk/compute", headers={"X-Tenant-Id": tenant}).json()
        assert posted["computable"] is True and posted["total_vol"] > 0
        # GET now computes from the cache the POST populated.
        got = client.get(f"/portfolios/{pid}/risk", headers={"X-Tenant-Id": tenant}).json()
        assert got["computable"] is True

    def test_risk_requires_ownership(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.get(f"/portfolios/{pid}/risk", headers={"X-Tenant-Id": str(uuid.uuid4())}).status_code == 404
