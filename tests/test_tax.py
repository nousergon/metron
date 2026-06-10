"""Tax lens — per-lot term + unrealized P&L + harvestable losses (C2-6e).

Open lots carry their open date + cost, so term (ST/LT) and unrealized P&L (at the
latest cached close) are fully determined. Invariants: term classification by holding
period; unrealized split ST/LT (Unknown→ST); below-cost lots flagged harvestable; a
lot with no cached price is excluded from totals (never fabricated).
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest

from api.services import tax
from portfolio_analytics.prices import ClosePoint

# AAPL bought 2024-01-01 (held > 1yr by mid-2025 → long-term); MSFT bought 2025-05-01
# (recent → short-term).
CSV = """date,type,symbol,quantity,price
2024-01-01,BUY,AAPL,10,100
2025-05-01,BUY,MSFT,5,200
"""

# AAPL +50% (gain), MSFT −10% (loss → harvestable).
def _prices(symbols, *, source=None):
    px = {"AAPL": 150.0, "MSFT": 180.0}
    return {s: ClosePoint(date(2025, 6, 1), px[s]) for s in symbols if s in px}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    assert client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    ).status_code == 200
    return pid


class TestTax:
    def test_unrealized_split_and_harvest(self, client, db_session, tenant, monkeypatch):
        pid = _seed(client, tenant)
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _prices)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
        client.post(f"/portfolios/{pid}/prices/refresh", headers={"X-Tenant-Id": tenant})

        s = tax.tax_lots(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2025, 6, 1))
        assert s.n_lots == 2 and s.n_priced == 2
        # AAPL: LT, 10×150−1000 = +500. MSFT: ST, 5×180−1000 = −100 (harvestable 100).
        assert s.unrealized_lt == pytest.approx(500.0)
        assert s.unrealized_st == pytest.approx(-100.0)
        assert s.unrealized_total == pytest.approx(400.0)
        assert s.harvestable_loss == pytest.approx(100.0)
        terms = {x.ticker: x.term for x in s.lots}
        assert terms == {"AAPL": "Long-term", "MSFT": "Short-term"}
        msft = next(x for x in s.lots if x.ticker == "MSFT")
        assert msft.harvestable_loss == pytest.approx(100.0)

    def test_price_free_lots_have_null_unrealized(self, client, db_session, tenant):
        pid = _seed(client, tenant)  # never refreshed → no prices
        s = tax.tax_lots(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2025, 6, 1))
        assert s.n_lots == 2 and s.n_priced == 0
        assert s.unrealized_total is None and s.unrealized_st is None
        assert all(x.market_value is None and x.unrealized_gain is None for x in s.lots)
        # Cost basis + term still present (price-free).
        assert all(x.cost_basis > 0 and x.term in {"Short-term", "Long-term"} for x in s.lots)

    def test_endpoint_and_ownership(self, client, tenant, monkeypatch):
        pid = _seed(client, tenant)
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _prices)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
        client.post(f"/portfolios/{pid}/prices/refresh", headers={"X-Tenant-Id": tenant})
        body = client.get(f"/portfolios/{pid}/tax", headers={"X-Tenant-Id": tenant}).json()
        assert body["n_lots"] == 2 and body["harvestable_loss"] == pytest.approx(100.0)
        assert client.get(f"/portfolios/{pid}/tax", headers={"X-Tenant-Id": str(uuid.uuid4())}).status_code == 404
