"""Router + entitlement tests for the plan-targets surface (metron-ops-I311):
GET/PUT round trip, the no-default-value invariant (intelligence-doctrine layer 2), the
422 when cash-to-targets runs with no saved targets, ownership 404, and the entitlement
catalog placement (both features ship in ``_BETA``, so the external demo sees them)."""

from __future__ import annotations

import uuid

import pytest

from api import entitlements as ent


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _portfolio(client, tenant):
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


class TestEntitlementCatalog:
    def test_both_features_are_in_beta_tier(self):
        feats = ent.resolve("beta", feed_enabled=False)
        by_key = {f["key"]: f for f in feats["features"]}
        assert by_key["cash_to_targets"]["available"] is True
        assert by_key["whatif_purchase"]["available"] is True

    def test_neither_feature_requires_a_data_source(self):
        assert ent.FEATURE_BY_KEY["cash_to_targets"].requires == ()
        assert ent.FEATURE_BY_KEY["whatif_purchase"].requires == ()


class TestTargetsRoundTrip:
    def test_get_with_nothing_saved_is_an_empty_no_default_state(self, client, tenant):
        pid = _portfolio(client, tenant)
        r = client.get(f"/portfolios/{pid}/plan/targets", headers=_hdr(tenant))
        assert r.status_code == 200
        body = r.json()
        assert body["targets"] == []
        assert body["max_single_position"] is None
        assert body["min_line_usd"] is None

    def test_put_then_get_round_trips_exactly(self, client, tenant):
        pid = _portfolio(client, tenant)
        payload = {
            "targets": [{"symbol": "AAPL", "weight": 0.3}, {"symbol": "MSFT", "weight": 0.2}],
            "max_single_position": 0.25,
            "min_line_usd": 100.0,
        }
        put = client.put(f"/portfolios/{pid}/plan/targets", json=payload, headers=_hdr(tenant))
        assert put.status_code == 200
        got = client.get(f"/portfolios/{pid}/plan/targets", headers=_hdr(tenant)).json()
        assert got["targets"] == [{"symbol": "AAPL", "weight": 0.3}, {"symbol": "MSFT", "weight": 0.2}]
        assert got["max_single_position"] == 0.25
        assert got["min_line_usd"] == 100.0

    def test_weights_over_100_percent_are_rejected(self, client, tenant):
        pid = _portfolio(client, tenant)
        payload = {"targets": [{"symbol": "AAPL", "weight": 0.7}, {"symbol": "MSFT", "weight": 0.5}]}
        r = client.put(f"/portfolios/{pid}/plan/targets", json=payload, headers=_hdr(tenant))
        assert r.status_code == 422

    def test_duplicate_symbol_is_rejected(self, client, tenant):
        pid = _portfolio(client, tenant)
        payload = {"targets": [{"symbol": "AAPL", "weight": 0.1}, {"symbol": "AAPL", "weight": 0.1}]}
        r = client.put(f"/portfolios/{pid}/plan/targets", json=payload, headers=_hdr(tenant))
        assert r.status_code == 422

    def test_foreign_portfolio_404s(self, client, tenant):
        other = str(uuid.uuid4())
        pid = _portfolio(client, other)
        r = client.get(f"/portfolios/{pid}/plan/targets", headers=_hdr(tenant))
        assert r.status_code == 404


class TestCashToTargetsRequiresSavedTargets:
    def test_422_with_no_targets_saved(self, client, tenant):
        pid = _portfolio(client, tenant)
        r = client.post(f"/portfolios/{pid}/plan/cash-to-targets", json={"amount_usd": 1000.0}, headers=_hdr(tenant))
        assert r.status_code == 422

    def test_runs_once_targets_are_saved(self, client, tenant):
        pid = _portfolio(client, tenant)
        client.put(
            f"/portfolios/{pid}/plan/targets",
            json={"targets": [{"symbol": "ZZZZ", "weight": 1.0}]},
            headers=_hdr(tenant),
        )
        r = client.post(f"/portfolios/{pid}/plan/cash-to-targets", json={"amount_usd": 1000.0}, headers=_hdr(tenant))
        assert r.status_code == 200
        body = r.json()
        # ZZZZ has no cached price in a fresh portfolio -> unpriced, unallocated in full.
        assert body["lines"] == []
        assert body["unallocated_usd"] == 1000.0
        assert body["disclaimer"] == "Arithmetic against the targets you set. Metron does not choose securities."


class TestWhatIf404sOffTier:
    def test_available_on_beta_by_default(self, client, tenant):
        pid = _portfolio(client, tenant)
        r = client.post(f"/portfolios/{pid}/plan/whatif", json={"symbol": "ZZZZ", "amount_usd": 100.0}, headers=_hdr(tenant))
        # No cached price and no feed by default settings -> 422 (needs a price), not 404
        # (the ENTITLEMENT gate is separate from the pricing gate — proves it isn't
        # tier-blocked on the base product).
        assert r.status_code in (200, 422)
        assert r.status_code != 404
