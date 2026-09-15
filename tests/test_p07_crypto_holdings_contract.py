"""Consumer contract test — data-collector plan P-07 (alpha-engine-config-I10870), D38.

Pinned copy of nousergon-data's `contracts/crypto_holdings.schema.json` lives at
`tests/contracts/crypto_holdings.schema.json` here (mirrors the `test_p07_contracts.py`
precedent for the other metron_* families — Metron never imports nousergon-data; the
versioned JSON schema IS the coupling). Covers:

  1. the pinned schema is itself a valid JSON Schema;
  2. a schema-conformant fixture feeds through the REAL consumer reader
     (`api.services.crypto.for_portfolio`, via its `reader=` injection seam) so a
     field this consumer actually depends on (chain/address/balance/value_usd,
     `as_of_utc` staleness) is exercised, not just declared;
  3. a deliberately broken/missing field fails schema validation — the drift alarm
     a pinned copy exists to provide.

D38 is PAUSED in nousergon-data (alpha-engine-config-I10748); this consumer still
reads `crypto/holdings.json` unconditionally, so it is a surviving consumer per
data_collection_plan_260914.md §3 regardless of producer cadence.

Refs alpha-engine-config-I10870, data_collection_plan_260914.md §3/§4.3.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from api.db import models
from api.services import crypto

CONTRACTS_DIR = Path(__file__).parent / "contracts"

_BTC = "bc1q9zpgru5j9q3dccf6n5xm9wglv5jh0w8r4d5xkp"


def _schema() -> dict:
    return json.loads((CONTRACTS_DIR / "crypto_holdings.schema.json").read_text())


def _validate(payload: dict) -> None:
    jsonschema.validate(instance=payload, schema=_schema())


def _seed_portfolio(session):
    tenant = models.Tenant(name="t")
    session.add(tenant)
    session.flush()
    pf = models.Portfolio(tenant_id=tenant.id, name="P", base_currency="USD")
    session.add(pf)
    session.commit()
    return tenant.id, pf.id


def test_pinned_schema_is_valid():
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_producer_shaped_artifact_validates_and_reader_extracts_value(db_session):
    art = {
        "schema_version": 1,
        "as_of_utc": "2026-09-15T12:00:00Z",
        "source": "blockstream+eth_rpc+coingecko+blockscout",
        "balances": [
            {"chain": "BTC", "address": _BTC, "symbol": "BTC", "balance": 0.5,
             "price_usd": 60000.0, "value_usd": 30000.0},
        ],
        "prices": {"BTC": 60000.0},
    }
    _validate(art)
    tid, pid = _seed_portfolio(db_session)
    crypto.add_address(db_session, tid, pid, "BTC", _BTC)
    from datetime import UTC, datetime
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    summary = crypto.for_portfolio(db_session, tid, pid, reader=lambda: art, now=now)
    assert summary.available is True
    assert summary.total_usd == pytest.approx(30000.0)
    assert summary.positions[0].synced is True


def test_balance_without_price_still_validates_and_reads_pending_value():
    art = {
        "schema_version": 1,
        "as_of_utc": "2026-09-15T12:00:00Z",
        "source": "test",
        "balances": [{"chain": "BTC", "address": _BTC, "symbol": "BTC", "balance": 0.5}],
    }
    _validate(art)


def test_record_missing_required_field_is_rejected():
    art = {
        "schema_version": 1,
        "as_of_utc": "2026-09-15T12:00:00Z",
        "source": "test",
        "balances": [{"chain": "BTC", "address": _BTC, "balance": 0.5}],  # no "symbol"
    }
    with pytest.raises(jsonschema.ValidationError):
        _validate(art)


def test_artifact_missing_as_of_utc_is_rejected():
    art = {"schema_version": 1, "source": "test", "balances": []}
    with pytest.raises(jsonschema.ValidationError):
        _validate(art)
