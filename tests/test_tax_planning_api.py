"""GET /tax-planning — entitlement gate + last-good/schema behavior (metron-ops#133)."""

from __future__ import annotations

import json

import pytest

from api.config import settings
from api.routers import tax_planning as router_mod
from portfolio_analytics.ingestion.tax_projection_store import (
    load_tax_projection,
    save_tax_projection,
    schema_error,
)


def _projection(schema_version: str = "1.0.0") -> dict:
    """A synthetic telos TaxProjection (contracts/tax_projection.schema.json, v1)."""
    return {
        "schema_version": schema_version,
        "tax_year": 2026,
        "as_of": "2026-07-03",
        "filing_status": "single",
        "pack_status": "provisional",
        "projected": {
            "agi": "296000",
            "taxable_income": "279900",
            "total_tax": "61852",
            "total_withholding": "40000",
            "estimated_payments_made": "5000",
            "balance_due": "16852",
            "effective_rate_on_agi": "0.2090",
            "marginal_ordinary_rate": "0.35",
        },
        "safe_harbor": {
            "basis": "90pct_current_year",
            "required_annual_payment": "55667",
            "total_estimated_tax_due": "15667",
        },
        "quarters": [
            {"quarter": 1, "due_date": "2026-04-15", "required": "3917",
             "paid": "5000", "shortfall": "0", "status": "paid"},
            {"quarter": 2, "due_date": "2026-06-15", "required": "3917",
             "paid": "0", "shortfall": "3917", "status": "overdue"},
            {"quarter": 3, "due_date": "2026-09-15", "required": "3917",
             "paid": "0", "shortfall": "3917", "status": "upcoming"},
            {"quarter": 4, "due_date": "2027-01-15", "required": "3916",
             "paid": "0", "shortfall": "3916", "status": "upcoming"},
        ],
        "headline": {
            "payment_recommended": True,
            "recommended_amount": "7834",
            "next_due_date": "2026-09-15",
            "message": "Pay 7834 (Q2 overdue (shortfall 3917); Q3 due 2026-09-15).",
        },
    }


@pytest.fixture()
def _cached(monkeypatch):
    """Serve a populated last-good projection without touching the filesystem."""
    monkeypatch.setattr(router_mod, "load_tax_projection", lambda **kw: _projection())


def test_entitled_tier_returns_projection(client, monkeypatch, _cached):
    monkeypatch.setattr(settings, "tier_simulator", False)
    body = client.get("/tax-planning").json()
    assert body["available"] is True
    assert body["stale"] is False
    assert body["schema_error"] is None
    proj = body["projection"]
    assert proj["projected"]["total_tax"] == "61852"
    assert proj["headline"]["payment_recommended"] is True
    assert [q["status"] for q in proj["quarters"]] == [
        "paid", "overdue", "upcoming", "upcoming",
    ]


def test_missing_artifact_is_stale_not_error(client, monkeypatch):
    monkeypatch.setattr(router_mod, "load_tax_projection", lambda **kw: None)
    body = client.get("/tax-planning").json()
    assert body["available"] is True
    assert body["stale"] is True
    assert body["projection"] is None
    assert body["schema_error"] is None


def test_unknown_schema_major_fails_loud_by_name(client, monkeypatch):
    """A 2.x artifact must produce a NAMED schema_error, never a mis-rendered panel."""
    monkeypatch.setattr(
        router_mod, "load_tax_projection", lambda **kw: _projection(schema_version="2.0.0")
    )
    body = client.get("/tax-planning").json()
    assert body["available"] is True
    assert body["projection"] is None
    assert "2.0.0" in body["schema_error"]
    assert "major 1" in body["schema_error"]


def test_additive_minor_bump_passes_through(client, monkeypatch):
    """1.1.0 (additive fields, e.g. the planned wa/ohio sections) must NOT be refused."""
    monkeypatch.setattr(
        router_mod, "load_tax_projection", lambda **kw: _projection(schema_version="1.1.0")
    )
    body = client.get("/tax-planning").json()
    assert body["schema_error"] is None
    assert body["projection"]["schema_version"] == "1.1.0"


# ── store ────────────────────────────────────────────────────────────────────


def test_store_round_trip_and_atomic_write(tmp_path):
    path = tmp_path / "tax_projection.json"
    save_tax_projection(_projection(), path=path)
    assert load_tax_projection(path=path)["tax_year"] == 2026
    assert not path.with_suffix(".json.tmp").exists()


def test_store_missing_and_corrupt_read_as_none(tmp_path):
    path = tmp_path / "tax_projection.json"
    assert load_tax_projection(path=path) is None
    path.write_text("{not json")
    assert load_tax_projection(path=path) is None
    path.write_text(json.dumps(["a", "list"]))
    assert load_tax_projection(path=path) is None


def test_schema_error_cases():
    assert schema_error(_projection()) is None
    assert schema_error(_projection("1.4.2")) is None
    assert "unsupported" in schema_error(_projection("2.0.0"))
    assert "no parseable schema_version" in schema_error({"tax_year": 2026})
