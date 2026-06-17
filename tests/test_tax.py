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

    def test_position_total_matches_lot_total_when_history_complete(self, client, db_session, tenant, monkeypatch):
        """With complete (ledger-reconstructable) history, the authoritative position-level
        total equals the lot-classified total and no gap is reported (metron-ops#65)."""
        pid = _seed(client, tenant)
        monkeypatch.setattr("api.services.prices.fetch_latest_closes", _prices)
        monkeypatch.setattr("api.services.performance.fetch_latest_closes", lambda s, *, source=None: {})
        client.post(f"/portfolios/{pid}/prices/refresh", headers={"X-Tenant-Id": tenant})
        s = tax.tax_lots(db_session, uuid.UUID(tenant), uuid.UUID(pid), today=date(2025, 6, 1))
        assert s.unrealized_position_total == pytest.approx(s.unrealized_total)
        assert s.n_incomplete == 0 and s.incomplete_tickers == []


def test_tax_total_reconciles_to_positions_when_lot_history_incomplete(db_session):
    """The bug: a broker-snapshot position whose activity feed starts mid-position is
    dropped from the lot ledger, so the lot-classified unrealized UNDER-counts. The tax
    total must reconcile to the position-level figure (what the Accounts panel shows) and
    surface the un-classifiable remainder rather than silently under-reporting (#65)."""
    from api.db import models

    tenant = models.Tenant(name="t-recon")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()

    # Ledger-sourced (CSV) account with a complete AAPL lot → lot-classifiable.
    csv_acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD",
    )
    aapl = models.Security(symbol="AAPL", currency="USD")
    db_session.add_all([csv_acct, aapl])
    db_session.flush()
    db_session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=csv_acct.id, security_id=aapl.id, txn_type="BUY",
            quantity=10, price=100.0, amount=1000.0, currency="USD",
            trade_date=date(2024, 1, 1), source_key="buy-aapl",
        )
    )
    db_session.add(models.PriceBar(security_id=aapl.id, bar_date=date(2025, 6, 1), close=150.0, currency="USD"))

    # Snapshot-sourced (SnapTrade) account with a position whose activity feed has only a
    # SELL (no opening BUY) → its lots can't be replayed → flagged incomplete.
    snap_acct = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade", external_id="ST-1",
        institution="E*TRADE", currency="USD",
    )
    sq = models.Security(symbol="SQ", currency="USD")
    db_session.add_all([snap_acct, sq])
    db_session.flush()
    db_session.add(
        models.Position(
            tenant_id=tenant.id, account_id=snap_acct.id, security_id=sq.id, quantity=27,
            avg_cost=50.0, currency="USD", market_price=60.0, market_value_local=1620.0,
            as_of=date(2025, 6, 1),
        )
    )
    db_session.add(
        models.Transaction(
            tenant_id=tenant.id, account_id=snap_acct.id, security_id=sq.id, txn_type="SELL",
            quantity=5, price=60.0, amount=300.0, currency="USD",
            trade_date=date(2025, 3, 1), source_key="sell-sq-orphan",
        )
    )
    db_session.commit()

    s = tax.tax_lots(db_session, tenant.id, pf.id, today=date(2025, 6, 1))
    # Lot-classified sees AAPL only: 10×150 − 1000 = +500.
    assert s.unrealized_total == pytest.approx(500.0)
    # Position-level reconciles to AAPL + the orphaned SQ position: 500 + (1620 − 1350) = 770.
    assert s.unrealized_position_total == pytest.approx(770.0)
    # The gap is surfaced, not swallowed.
    assert s.n_incomplete == 1
    assert s.incomplete_tickers == ["SQ"]
