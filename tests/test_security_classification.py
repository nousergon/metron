"""User-set GICS-sector / country-of-domicile overrides — fills/corrects the Unclassified
gap for a holding the data spine couldn't classify. Tenant-scoped, per-field, clearable;
overlaid onto holdings (override wins over the spine-resolved value)."""

from __future__ import annotations

import io
import uuid

import pytest

# A numeric-CUSIP bond the spine can't classify → sector/country stay null until overridden.
CSV = "date,type,symbol,quantity,price,amount,account\n2024-01-01,BUY,912828YK0,50,98,4900,Brokerage\n"


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed(client, tenant: str) -> str:
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    return pid


def _classification_of(client, tenant, pid, symbol) -> tuple:
    holdings = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    h = next((h for h in holdings if h["ticker"] == symbol), None)
    assert h is not None, "holding missing"
    return h["sector"], h["country"]


def test_set_and_clear_classification(client, tenant):
    pid = _seed(client, tenant)
    assert _classification_of(client, tenant, pid, "912828YK0") == (None, None)

    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"sector": "Industrials", "country": "United States"},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    assert r.json() == {"symbol": "912828YK0", "sector": "Industrials", "country": "United States", "instrument_type": None}
    assert _classification_of(client, tenant, pid, "912828YK0") == ("Industrials", "United States")

    # Clearing only the sector leaves the country untouched (per-field).
    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification", json={"sector": ""}, headers=_hdr(tenant)
    )
    assert r.json() == {"symbol": "912828YK0", "sector": None, "country": "United States", "instrument_type": None}
    assert _classification_of(client, tenant, pid, "912828YK0") == (None, "United States")

    # Clearing the last remaining field removes the override entirely.
    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification", json={"country": None}, headers=_hdr(tenant)
    )
    assert r.json() == {"symbol": "912828YK0", "sector": None, "country": None, "instrument_type": None}
    assert _classification_of(client, tenant, pid, "912828YK0") == (None, None)


def test_omitted_field_is_left_untouched(client, tenant):
    pid = _seed(client, tenant)
    client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"sector": "Industrials", "country": "United States"},
        headers=_hdr(tenant),
    )
    # A patch that carries ONLY sector must not clear the previously-set country.
    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification", json={"sector": "Financial Services"}, headers=_hdr(tenant)
    )
    assert r.json() == {"symbol": "912828YK0", "sector": "Financial Services", "country": "United States", "instrument_type": None}


def test_symbol_normalized_lowercase(client, tenant):
    pid = _seed(client, tenant)
    client.put(
        f"/portfolios/{pid}/securities/912828yk0/classification", json={"country": "Canada"}, headers=_hdr(tenant)
    )
    assert _classification_of(client, tenant, pid, "912828YK0") == (None, "Canada")


def _security_type_of(client, tenant, pid, symbol) -> str:
    holdings = client.get(f"/portfolios/{pid}/holdings", headers=_hdr(tenant)).json()
    h = next((h for h in holdings if h["ticker"] == symbol), None)
    assert h is not None, "holding missing"
    return h["security_type"]


def test_instrument_type_override_changes_the_holding_type(client, tenant):
    pid = _seed(client, tenant)
    # The alphanumeric-CUSIP holding the spine can't classify defaults to equity.
    assert _security_type_of(client, tenant, pid, "912828YK0") == "equity"
    # Override it to a Treasury (the real instrument) — the holding's type follows.
    r = client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"instrument_type": "treasury"},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    assert r.json()["instrument_type"] == "treasury"
    assert _security_type_of(client, tenant, pid, "912828YK0") == "treasury"
    # Clearing it reverts to the classified default.
    client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"instrument_type": ""},
        headers=_hdr(tenant),
    )
    assert _security_type_of(client, tenant, pid, "912828YK0") == "equity"


def test_unknown_instrument_type_rejected(client, tenant):
    pid = _seed(client, tenant)
    assert client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"instrument_type": "bogus"},
        headers=_hdr(tenant),
    ).status_code == 422


def test_classification_is_tenant_scoped(client, tenant):
    pid = _seed(client, tenant)
    other = str(uuid.uuid4())
    # Another tenant can't even see this portfolio (404 — never leaks cross-tenant).
    assert client.put(
        f"/portfolios/{pid}/securities/912828YK0/classification",
        json={"country": "Hijack"},
        headers=_hdr(other),
    ).status_code == 404
