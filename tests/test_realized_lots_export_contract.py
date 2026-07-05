"""Consumer contract test — the realized-lots export conforms to the telos
``realized_lots`` contract v1.0.0 (metron-ops#127; consumer telos#6).

Metron never imports telos: the versioned JSON schema is the entire coupling. A copy of
``nousergon/telos:contracts/realized_lots.schema.json`` is pinned at
``tests/contracts/realized_lots.schema.json`` and validated here with jsonschema, mirroring
the telos generation test so the export can't silently drift from the contract. If telos
ships a new schema version, re-pin the file and this test fails loudly until the export
matches — exactly the drift alarm the pinned copy exists to provide.

Three layers:
  1. the pinned schema is itself a valid JSON Schema (guards a bad re-pin);
  2. the pure ``domain.realized_lots_export`` output validates and boxes/terms correctly;
  3. the live API export (taxable-only, per-year) validates and excludes tax-advantaged lots.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import date
from pathlib import Path

import jsonschema
import pytest

from portfolio_analytics.domain import realized_lots_export as rle

SCHEMA_PATH = Path(__file__).parent / "contracts" / "realized_lots.schema.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validate(payload: dict, schema: dict) -> None:
    """Raise if ``payload`` doesn't satisfy the telos realized_lots contract."""
    jsonschema.validate(instance=payload, schema=schema)


# ── 1. the pinned schema is a valid JSON Schema, pinned at the version we built against ──


def test_pinned_schema_is_valid_and_v1(schema):
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["properties"]["schema_version"]["const"] == "1.0.0"
    # The box enum we assign into must be the contract's exact letter set (A–L).
    assert set(schema["$defs"]["Form8949Box"]["enum"]) == set("ABCDEFGHIJKL")
    assert rle.SCHEMA_VERSION == schema["properties"]["schema_version"]["const"]


# ── 2. the pure domain projection ──────────────────────────────────────────────────────


def _lot(**kw) -> rle.ExportLot:
    base = dict(
        description="AAPL",
        date_acquired=date(2023, 1, 2),
        date_sold=date(2024, 6, 3),
        proceeds=1500.0,
        cost_basis=1000.0,
        long_term=True,
        source="ibkr_flex",
    )
    base.update(kw)
    return rle.ExportLot(**base)


def test_domain_export_validates_and_boxes(schema):
    lots = [
        _lot(long_term=False),                                   # equity short → A
        _lot(long_term=True),                                    # equity long  → D
        _lot(description="BTC", long_term=False, asset_class="crypto"),   # crypto short → G
        _lot(description="ETH", long_term=True, asset_class="crypto"),    # crypto long  → J
        _lot(date_acquired=None),                                # unknown acquisition → VARIOUS
    ]
    payload = rle.build_export(lots)
    _validate(payload, schema)

    assert payload["schema_version"] == "1.0.0"
    boxes = [row["box"] for row in payload["lots"]]
    assert boxes == ["A", "D", "G", "J", "D"]
    terms = [row["term"] for row in payload["lots"]]
    assert terms == ["short", "long", "short", "long", "long"]
    assert payload["lots"][4]["date_acquired"] == "VARIOUS"
    assert payload["lots"][0]["date_acquired"] == "2023-01-02"
    assert payload["lots"][0]["date_sold"] == "2024-06-03"
    # Default wash-sale adjustment when Metron carries none — never recomputed.
    assert all(row["wash_sale_disallowed"] == 0.0 for row in payload["lots"])


def test_empty_export_validates(schema):
    _validate(rle.build_export([]), schema)


def test_proceeds_and_basis_are_nonnegative_numbers(schema):
    # A loss lot (proceeds < basis) is still two non-negative fields — the loss is implicit,
    # never a negative column value the schema would reject.
    payload = rle.build_export([_lot(proceeds=800.0, cost_basis=1000.0)])
    _validate(payload, schema)
    assert payload["lots"][0]["proceeds"] == 800.0
    assert payload["lots"][0]["cost_basis"] == 1000.0


# ── 3. the live API export ──────────────────────────────────────────────────────────────

# Brokerage (taxable): AAPL bought 2023, sold 2024 → a LONG-term lot in tax year 2024.
# IRA (tax-advantaged): MSFT sold 2024 → must NOT appear (never a taxable disposal).
# A 2025 Brokerage sale must NOT appear in the 2024 export (per-year filter).
CSV = (
    "date,type,symbol,quantity,price,amount,account\n"
    "2023-01-02,BUY,AAPL,10,100,1000,Brokerage\n"
    "2024-06-03,SELL,AAPL,10,150,1500,Brokerage\n"
    "2023-01-02,BUY,NVDA,10,100,1000,Brokerage\n"
    "2025-02-01,SELL,NVDA,10,150,1500,Brokerage\n"
    "2023-01-02,BUY,MSFT,10,200,2000,IRA\n"
    "2024-06-03,SELL,MSFT,10,300,3000,IRA\n"
)


@pytest.fixture()
def tenant() -> str:
    return str(uuid.uuid4())


def _hdr(t: str) -> dict:
    return {"X-Tenant-Id": t}


def _seed(client, tenant: str) -> str:
    pid = client.post("/portfolios", json={"name": "P"}, headers=_hdr(tenant)).json()["id"]
    r = client.post(
        f"/portfolios/{pid}/import/csv",
        files={"file": ("t.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers=_hdr(tenant),
    )
    assert r.status_code == 200
    return pid


def test_api_export_validates_against_contract(client, tenant, schema):
    pid = _seed(client, tenant)
    r = client.get(f"/portfolios/{pid}/realized-lots-export?year=2024", headers=_hdr(tenant))
    assert r.status_code == 200
    payload = r.json()
    _validate(payload, schema)

    assert payload["schema_version"] == "1.0.0"
    # Only the taxable Brokerage AAPL lot closed in 2024 — IRA MSFT excluded, 2025 NVDA excluded.
    assert len(payload["lots"]) == 1
    lot = payload["lots"][0]
    assert lot["description"] == "AAPL"
    assert lot["date_sold"] == "2024-06-03"
    assert lot["term"] == "long"          # held > 1yr
    assert lot["box"] == "D"              # equity, long, basis-reported
    assert float(lot["proceeds"]) == 1500.0
    assert float(lot["cost_basis"]) == 1000.0
    assert lot["source"] == "csv"         # the connector that produced the lot


def test_api_export_empty_year_validates(client, tenant, schema):
    pid = _seed(client, tenant)
    r = client.get(f"/portfolios/{pid}/realized-lots-export?year=2019", headers=_hdr(tenant))
    assert r.status_code == 200
    payload = r.json()
    _validate(payload, schema)
    assert payload["lots"] == []
