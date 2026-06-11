"""Settings API — base currency, account tags (institution/type/taxable override),
and investor preferences."""

from __future__ import annotations

import io
import uuid

import pytest

CSV = """date,type,symbol,quantity,price
2024-01-01,BUY,AAPL,10,100
"""


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _seed(client, tenant):
    pid = client.post("/portfolios", json={"name": "P"}, headers={"X-Tenant-Id": tenant}).json()["id"]
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers={"X-Tenant-Id": tenant},
    )
    return pid


class TestBaseCurrency:
    def test_patch_base_currency(self, client, tenant):
        pid = _seed(client, tenant)
        r = client.patch(f"/portfolios/{pid}", json={"base_currency": "eur"}, headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200 and r.json()["base_currency"] == "EUR"

    def test_rename_still_works(self, client, tenant):
        pid = _seed(client, tenant)
        r = client.patch(f"/portfolios/{pid}", json={"name": "Renamed"}, headers={"X-Tenant-Id": tenant})
        assert r.status_code == 200 and r.json()["name"] == "Renamed"

    def test_empty_patch_rejected(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.patch(f"/portfolios/{pid}", json={}, headers={"X-Tenant-Id": tenant}).status_code == 422

    def test_bad_currency_rejected(self, client, tenant):
        pid = _seed(client, tenant)
        assert client.patch(f"/portfolios/{pid}", json={"base_currency": "DOLLARS"}, headers={"X-Tenant-Id": tenant}).status_code == 422


class TestAccountTags:
    def test_patch_tags_and_taxable_override(self, client, tenant):
        pid = _seed(client, tenant)
        acct_id = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()[0]["account_id"]
        # CSV account defaults taxable; override to tax-advantaged + set tags.
        r = client.patch(
            f"/portfolios/{pid}/accounts/{acct_id}",
            json={"institution": "Fidelity", "account_type": "Roth IRA", "taxable_override": False},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["institution"] == "Fidelity"
        assert body["account_type"] == "Roth IRA"
        assert body["taxable"] is False
        # Revert override to auto → name/type keywords now drive it (Roth IRA → not taxable).
        r2 = client.patch(
            f"/portfolios/{pid}/accounts/{acct_id}",
            json={"taxable_override": None},
            headers={"X-Tenant-Id": tenant},
        )
        assert r2.json()["taxable"] is False  # auto-derived from "Roth IRA" type

    def test_partial_patch_leaves_other_fields(self, client, tenant):
        pid = _seed(client, tenant)
        acct_id = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()[0]["account_id"]
        client.patch(f"/portfolios/{pid}/accounts/{acct_id}", json={"institution": "Schwab"}, headers={"X-Tenant-Id": tenant})
        r = client.patch(f"/portfolios/{pid}/accounts/{acct_id}", json={"account_type": "Brokerage"}, headers={"X-Tenant-Id": tenant})
        assert r.json()["institution"] == "Schwab" and r.json()["account_type"] == "Brokerage"

    def test_cross_tenant_404(self, client, tenant):
        pid = _seed(client, tenant)
        acct_id = client.get(f"/portfolios/{pid}/accounts", headers={"X-Tenant-Id": tenant}).json()[0]["account_id"]
        r = client.patch(f"/portfolios/{pid}/accounts/{acct_id}", json={"institution": "X"}, headers={"X-Tenant-Id": str(uuid.uuid4())})
        assert r.status_code == 404


class TestPreferences:
    def test_get_defaults_then_put(self, client, tenant):
        pid = _seed(client, tenant)
        d = client.get(f"/portfolios/{pid}/preferences", headers={"X-Tenant-Id": tenant}).json()
        assert d == {"risk_tolerance": None, "objective": None, "notes": None, "snaptrade_institutions": None}
        r = client.put(
            f"/portfolios/{pid}/preferences",
            json={"risk_tolerance": "moderate", "objective": "growth", "notes": "buy and hold"},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 200 and r.json()["risk_tolerance"] == "moderate"
        # Persisted + idempotent update.
        again = client.get(f"/portfolios/{pid}/preferences", headers={"X-Tenant-Id": tenant}).json()
        assert again["objective"] == "growth" and again["notes"] == "buy and hold"
