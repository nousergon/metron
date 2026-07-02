"""Saved Holdings-table view preferences (metron-ops#114): grouping / visible bands /
combine, persisted on InvestorPreferences and hydrated on the Holdings page."""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture()
def tenant():
    return str(uuid.uuid4())


def _hdr(tenant):
    return {"X-Tenant-Id": tenant}


def _portfolio(client, tenant):
    return client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]


def test_unset_view_is_all_null(client, tenant):
    pid = _portfolio(client, tenant)
    v = client.get(f"/portfolios/{pid}/holdings-view", headers=_hdr(tenant)).json()
    assert v == {"grouping": None, "visible_bands": None, "combine_by_account": None, "hidden_types": None}


def test_put_then_get_roundtrips(client, tenant):
    pid = _portfolio(client, tenant)
    body = {
        "grouping": "account",
        "visible_bands": ["Attractiveness", "Valuation"],
        "combine_by_account": True,
        "hidden_types": ["cash", "option"],
    }
    put = client.put(f"/portfolios/{pid}/holdings-view", json=body, headers=_hdr(tenant))
    assert put.status_code == 200
    got = client.get(f"/portfolios/{pid}/holdings-view", headers=_hdr(tenant)).json()
    assert got == body


def test_unknown_hidden_type_is_dropped_not_rejected(client, tenant):
    pid = _portfolio(client, tenant)
    r = client.put(
        f"/portfolios/{pid}/holdings-view",
        json={"hidden_types": ["bond", "crypto"]},  # crypto isn't a known type
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    assert r.json()["hidden_types"] == ["bond"]  # unknown silently filtered


def test_null_fields_clear_back_to_default(client, tenant):
    pid = _portfolio(client, tenant)
    client.put(
        f"/portfolios/{pid}/holdings-view",
        json={"grouping": "asset", "visible_bands": ["Attractiveness"], "combine_by_account": True, "hidden_types": ["cash"]},
        headers=_hdr(tenant),
    )
    client.put(
        f"/portfolios/{pid}/holdings-view",
        json={"grouping": None, "visible_bands": None, "combine_by_account": None, "hidden_types": None},
        headers=_hdr(tenant),
    )
    got = client.get(f"/portfolios/{pid}/holdings-view", headers=_hdr(tenant)).json()
    assert got == {"grouping": None, "visible_bands": None, "combine_by_account": None, "hidden_types": None}


def test_invalid_grouping_and_band_rejected(client, tenant):
    pid = _portfolio(client, tenant)
    assert client.put(
        f"/portfolios/{pid}/holdings-view", json={"grouping": "bogus"}, headers=_hdr(tenant)
    ).status_code == 422
    assert client.put(
        f"/portfolios/{pid}/holdings-view", json={"visible_bands": ["Nope"]}, headers=_hdr(tenant)
    ).status_code == 422
