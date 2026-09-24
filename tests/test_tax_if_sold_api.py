"""'If sold' tax preview — service + read-only endpoint (metron-ops#208).

``GET /portfolios/{id}/tax/if-sold`` measures a hypothetical sale the user authors. The
invariants here are the API's own: taxable-only scope by default (same as ``GET /tax``),
quantity defaults to the full position and price to the latest cached close, rates the
caller leaves out fall back to named placeholders, a specific-lot request carries the
FIFO baseline + delta, bad input is a 422, and the endpoint writes nothing.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from api.db import models
from api.db.session import Base
from api.services import tax

TODAY = date.today()


def _seed(db_session, *, price: bool = True, incomplete_sq: bool = False) -> tuple[str, str]:
    """Taxable account: AAPL 10 @100 (≈800d, LT), 5 @150 (≈450d, LT), 10 @200 (100d, ST).
    IRA account: AAPL 7 @50 — excluded from the taxable-only default."""
    tenant = models.Tenant(name=f"t-{uuid.uuid4()}")
    db_session.add(tenant)
    db_session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    db_session.add(pf)
    db_session.flush()
    brokerage = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-1", currency="USD",
        tax_treatment="taxable",
    )
    ira = models.Account(
        tenant_id=tenant.id, portfolio_id=pf.id, broker="csv", external_id="CSV-IRA", currency="USD",
        tax_treatment="tax_deferred",
    )
    aapl = db_session.scalar(select(models.Security).where(models.Security.symbol == "AAPL"))
    if aapl is None:
        aapl = models.Security(symbol="AAPL", currency="USD")
    db_session.add_all([brokerage, ira, aapl])
    db_session.flush()

    def _buy(account, sec, qty, px, days_ago, key):
        db_session.add(
            models.Transaction(
                tenant_id=tenant.id, account_id=account.id, security_id=sec.id, txn_type="BUY",
                quantity=qty, price=px, amount=qty * px, currency="USD",
                trade_date=TODAY - timedelta(days=days_ago), source_key=key,
            )
        )

    _buy(brokerage, aapl, 10, 100.0, 800, "b1")
    _buy(brokerage, aapl, 5, 150.0, 450, "b2")
    _buy(brokerage, aapl, 10, 200.0, 100, "b3")
    _buy(ira, aapl, 7, 50.0, 900, "b4")
    if price:
        db_session.add(
            models.PriceBar(security_id=aapl.id, bar_date=TODAY - timedelta(days=1), close=180.0, currency="USD")
        )
    if incomplete_sq:
        # A second taxable account whose AAPL feed starts with a SELL (no opening BUY) —
        # those shares can't be dated, so the preview flags the gap.
        other = models.Account(
            tenant_id=tenant.id, portfolio_id=pf.id, broker="snaptrade", external_id="ST-1", currency="USD",
            tax_treatment="taxable",
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            models.Transaction(
                tenant_id=tenant.id, account_id=other.id, security_id=aapl.id, txn_type="SELL",
                quantity=3, price=170.0, amount=510.0, currency="USD",
                trade_date=TODAY - timedelta(days=10), source_key="s1",
            )
        )
    db_session.commit()
    return str(tenant.id), str(pf.id)


def _get(client, tenant, pid, **params):
    return client.get(f"/portfolios/{pid}/tax/if-sold", params=params, headers={"X-Tenant-Id": tenant})


class TestEndpoint:
    def test_defaults_full_taxable_position_at_latest_close_with_placeholder_rates(self, client, db_session):
        tenant, pid = _seed(db_session)
        r = _get(client, tenant, pid, ticker="AAPL")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ticker"] == "AAPL" and body["currency"] == "USD"
        assert body["quantity_held"] == pytest.approx(25.0)  # IRA's 7 excluded
        assert body["n_accounts_excluded"] == 1
        assert body["price"] == 180.0 and body["price_source"] == "latest_close"
        assert body["price_as_of"] == (TODAY - timedelta(days=1)).isoformat()
        assert body["rates_placeholder"] == ["short_term", "long_term", "state"]
        assert body["rates"] == {"short_term": 0.24, "long_term": 0.15, "state": 0.0}
        sale = body["sale"]
        assert sale["method"] == "fifo" and sale["quantity"] == pytest.approx(25.0)
        assert sale["gain_lt"] == pytest.approx(800 + 150)
        assert sale["gain_st"] == pytest.approx(-200.0)
        assert sale["taxable_lt"] == pytest.approx(750.0)
        assert sale["est_tax_total"] == pytest.approx(750 * 0.15)
        assert [lot["term"] for lot in sale["lots"]] == ["Long-term", "Long-term", "Short-term"]
        assert body["fifo"] == sale and body["delta_vs_fifo"] is None
        assert [lot["lot_index"] for lot in body["open_lots"]] == [0, 1, 2]
        assert body["history_incomplete"] is False

    def test_taxable_only_false_includes_tax_advantaged_lots(self, client, db_session):
        tenant, pid = _seed(db_session)
        body = _get(client, tenant, pid, ticker="AAPL", taxable_only="false").json()
        assert body["quantity_held"] == pytest.approx(32.0)
        assert body["n_accounts_excluded"] == 0

    def test_user_rates_price_and_quantity(self, client, db_session):
        tenant, pid = _seed(db_session)
        body = _get(
            client, tenant, pid, ticker="aapl", quantity=12, price=190, st_rate=0.35, lt_rate=0.2, state_rate=0.05,
        ).json()
        assert body["price_source"] == "user" and body["price_as_of"] is None
        assert body["rates_placeholder"] == []
        # FIFO 12 @190: lot 0 10×90 + lot 1 2×40 = 980 LT.
        assert body["sale"]["gain_lt"] == pytest.approx(980.0)
        assert body["sale"]["est_tax_total"] == pytest.approx(980 * 0.2 + 980 * 0.05)

    def test_partial_rates_flag_only_the_missing_ones(self, client, db_session):
        tenant, pid = _seed(db_session)
        body = _get(client, tenant, pid, ticker="AAPL", st_rate=0.3).json()
        assert body["rates_placeholder"] == ["long_term", "state"]
        assert body["rates"]["short_term"] == 0.3

    def test_specific_lots_carry_fifo_baseline_and_delta(self, client, db_session):
        tenant, pid = _seed(db_session)
        r = client.get(
            f"/portfolios/{pid}/tax/if-sold?ticker=AAPL&quantity=12&method=specific&lot=2:10&lot=1:2",
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sale"]["method"] == "specific"
        assert [(x["lot_index"], x["quantity"]) for x in body["sale"]["lots"]] == [(2, 10.0), (1, 2.0)]
        assert body["fifo"]["method"] == "fifo"
        assert [(x["lot_index"], x["quantity"]) for x in body["fifo"]["lots"]] == [(0, 10.0), (1, 2.0)]
        delta = body["delta_vs_fifo"]
        assert delta["gain_total"] == pytest.approx(body["sale"]["gain_total"] - body["fifo"]["gain_total"])
        assert delta["est_tax_total"] == pytest.approx(-body["fifo"]["est_tax_total"])

    @pytest.mark.parametrize(
        ("query", "fragment"),
        [
            ("ticker=AAPL&method=specific&lot=nope", "<index>:<quantity>"),
            ("ticker=AAPL&method=specific&lot=a:1", "<index>:<quantity>"),
            ("ticker=AAPL&quantity=12&method=specific&lot=0:5", "not the 12"),
            ("ticker=AAPL&method=specific", "at least one lot"),
            ("ticker=AAPL&quantity=99", "exceeds"),
            ("ticker=MSFT", "No open lots for MSFT"),
        ],
    )
    def test_unmeasurable_hypotheticals_are_422(self, client, db_session, query, fragment):
        tenant, pid = _seed(db_session)
        r = client.get(f"/portfolios/{pid}/tax/if-sold?{query}", headers={"X-Tenant-Id": tenant})
        assert r.status_code == 422
        assert fragment in r.json()["detail"]

    @pytest.mark.parametrize("query", ["ticker=AAPL&st_rate=1.5", "ticker=AAPL&quantity=0", "ticker=AAPL&price=-1", ""])
    def test_request_validation(self, client, db_session, query):
        tenant, pid = _seed(db_session)
        assert client.get(f"/portfolios/{pid}/tax/if-sold?{query}", headers={"X-Tenant-Id": tenant}).status_code == 422

    def test_no_cached_close_asks_for_a_price(self, client, db_session):
        tenant, pid = _seed(db_session, price=False)
        r = _get(client, tenant, pid, ticker="AAPL")
        assert r.status_code == 422 and "enter a price" in r.json()["detail"]
        assert _get(client, tenant, pid, ticker="AAPL", price=150).status_code == 200

    def test_ownership(self, client, db_session):
        _, pid = _seed(db_session)
        assert _get(client, str(uuid.uuid4()), pid, ticker="AAPL").status_code == 404

    def test_entitlement_gate(self, client, db_session, monkeypatch):
        tenant, pid = _seed(db_session)
        monkeypatch.setattr(
            "api.routers.portfolios._effective_entitlement",
            lambda key, tier, feed: {"key": key, "available": False, "reason": "tier", "required_tier": "personal"},
        )
        r = _get(client, tenant, pid, ticker="AAPL")
        assert r.status_code == 404

    def test_endpoint_writes_nothing(self, client, db_session):
        tenant, pid = _seed(db_session)

        def _counts():
            return {t.name: db_session.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables}

        before = _counts()
        assert _get(client, tenant, pid, ticker="AAPL").status_code == 200
        assert client.get(
            f"/portfolios/{pid}/tax/if-sold?ticker=AAPL&quantity=3&method=specific&lot=0:3",
            headers={"X-Tenant-Id": tenant},
        ).status_code == 200
        db_session.expire_all()
        assert _counts() == before

    def test_incomplete_history_is_flagged(self, client, db_session):
        tenant, pid = _seed(db_session, incomplete_sq=True)
        body = _get(client, tenant, pid, ticker="AAPL").json()
        assert body["history_incomplete"] is True
        assert body["quantity_held"] == pytest.approx(25.0)


class TestService:
    def test_no_lots_with_incomplete_history_explains_why(self, db_session):
        tenant, pid = _seed(db_session, incomplete_sq=True)
        # Scope to the account holding only the undatable SELL.
        other = db_session.scalar(select(models.Account).where(models.Account.external_id == "ST-1",
                                                               models.Account.portfolio_id == uuid.UUID(pid)))
        with pytest.raises(tax.IfSoldError, match="starts mid-position"):
            tax.if_sold_preview(
                db_session, uuid.UUID(tenant), uuid.UUID(pid), "AAPL", today=TODAY,
                rates=tax.PLACEHOLDER_RATES, selected_account_ids={other.id},
            )

    def test_no_lots_outside_taxable_scope_names_the_scope(self, db_session):
        tenant, pid = _seed(db_session)
        with pytest.raises(tax.IfSoldError, match="accounts in scope"):
            tax.if_sold_preview(
                db_session, uuid.UUID(tenant), uuid.UUID(pid), "TSLA", today=TODAY,
                rates=tax.PLACEHOLDER_RATES, taxable_only=False,
            )

    def test_blank_ticker(self, db_session):
        tenant, pid = _seed(db_session)
        with pytest.raises(tax.IfSoldError, match="Enter a ticker"):
            tax.if_sold_preview(db_session, uuid.UUID(tenant), uuid.UUID(pid), "  ", today=TODAY,
                                rates=tax.PLACEHOLDER_RATES)

    def test_resolve_rates(self):
        rates, placeholder = tax.resolve_rates(None, 0.2, None)
        assert (rates.short_term, rates.long_term, rates.state) == (0.24, 0.2, 0.0)
        assert placeholder == ["short_term", "state"]
        with pytest.raises(tax.IfSoldError, match="short_term"):
            tax.resolve_rates(2.0, None, None)
