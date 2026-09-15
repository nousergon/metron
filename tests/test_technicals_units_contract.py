"""Consumer contract test -- data-collector plan P-14 (alpha-engine-config-I10781).

Pinned copy of nousergon-data's `market_data/technicals/latest.json` field
contract, in `tests/contracts/technicals_snapshot.schema.json` (mirrors the
P-07 precedent: metron-PR457, crucible-PR302 -- Metron has no importable
dependency on nousergon-data, so the versioned pinned schema IS the
coupling).

**Why this artifact, not `features/registry.py::CATALOG`.** P-14's stated
goal is a consumer contract test that fails on a suffix/type change to "any
feature column [Metron] reads". Grepped 2026-09-14 (`api/services/*.py`,
`portfolio_analytics/`): Metron reads NO `features/{date}/*.parquet`
snapshot and NO `registry.CATALOG`-named column, directly or transitively --
`market_data/fundamentals/latest.json` is raw yfinance data (a different
collector path, not `feature_engineer.compute_features`), and
`factors/profiles/latest.json` carries `nousergon_lib.quant.attractiveness`
pillar COMPOSITES (`quality_score`, `value_score`, ...) computed by
crucible-research from CATALOG z-scores, not CATALOG column names
themselves. `market_data/technicals/latest.json` is the one real,
measured coupling: `collectors/metron_market_data.py::collect_technicals`
computes it via `features.feature_engineer._compute_rsi` /
`_compute_macd` -- the SAME functions the CATALOG-governed write path
calls -- under separately-named fields. A units/scale drift here (a
fraction silently becoming a percent, or a price becoming a ratio) is the
`avg_volume_20d` defect class one hop from CATALOG naming rather than
through it, and this file pins THAT boundary rather than fabricating a
dependency on `registry.CATALOG` names Metron does not actually read.

Each test:
  1. checks the pinned schema is itself a valid JSON Schema;
  2. checks the pin's property set is EXACTLY `TickerTechnicals`'s field set
     (minus `yf_symbol`) -- drift between the pin and the dataclass fails;
  3. feeds a schema-conformant fixture through the REAL consumer reader
     (`load_technicals`, injected `reader=`) so a field this consumer
     depends on is exercised, not just declared;
  4. asserts a deliberately out-of-range / wrong-scale / wrong-type field
     fails schema validation -- the drift alarm a pinned copy exists to
     provide.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import jsonschema

from api.services.technicals import TickerTechnicals, load_technicals

CONTRACTS_DIR = Path(__file__).parent / "contracts"
SCHEMA_PATH = CONTRACTS_DIR / "technicals_snapshot.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


#: A realistic, schema-conformant fixture for one symbol.
_VALID_FIXTURE: dict[str, float] = {
    "rsi_14": 62.3,
    "macd_hist": 1.85,
    "ma_50": 201.4,
    "ma_200": 188.9,
    "pct_to_ma_50": 0.021,
    "pct_to_ma_200": 0.093,
    "high_52w": 228.0,
    "low_52w": 152.1,
    "pct_in_52w_range": 0.61,
    "pct_from_52wk_high": -0.084,
    "mom_20d": 0.034,
    "mom_60d": -0.012,
}


def _artifact(body: dict) -> dict:
    return {"schema_version": 1, "as_of": "2026-09-12", "technicals": {"AAPL": body}}


def test_the_pinned_schema_is_itself_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_the_pin_names_exactly_the_ticker_technicals_fields():
    """Drift in EITHER direction is a failure: a field `TickerTechnicals`
    carries that the pin doesn't know about, or a pinned field the
    dataclass no longer carries."""
    dataclass_fields = {f.name for f in dataclasses.fields(TickerTechnicals)} - {"yf_symbol"}
    pinned_fields = set(_schema()["properties"])
    assert pinned_fields == dataclass_fields, (
        f"pin vs TickerTechnicals mismatch -- pinned only: "
        f"{sorted(pinned_fields - dataclass_fields)}, dataclass only: "
        f"{sorted(dataclass_fields - pinned_fields)}"
    )


def test_a_valid_fixture_validates_and_the_real_reader_parses_it():
    jsonschema.validate(instance=_VALID_FIXTURE, schema=_schema())

    def _reader():
        return _artifact(_VALID_FIXTURE)

    snapshot = load_technicals(reader=_reader)
    parsed = snapshot.by_symbol["AAPL"]
    assert parsed.rsi_14 == 62.3
    assert parsed.pct_in_52w_range == 0.61


def test_rsi_14_above_100_fails_schema():
    """A wrong-scale/out-of-range rsi -- the RSI(14) contract is 0-100 by
    construction; a value above it means the producer stopped clamping or
    started emitting something else under the same name."""
    bad = dict(_VALID_FIXTURE, rsi_14=145.0)
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "rsi_14 above 100 must fail the pinned schema"


def test_pct_in_52w_range_as_a_percent_instead_of_a_fraction_fails_schema():
    """The exact avg_volume_20d shape: `pct_in_52w_range` is documented as a
    0-1 decimal fraction. If nousergon-data started emitting it as a 0-100
    percent under the SAME field name -- no rename, no CI signal in this
    repo without this pin -- 61.0 (meant as 61%) silently reads as 61x the
    range height instead of 61% of it."""
    bad = dict(_VALID_FIXTURE, pct_in_52w_range=61.0)
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "pct_in_52w_range expressed as a percent must fail the pinned schema"


def test_a_string_typed_field_fails_schema():
    """Type change, not just scale: a field silently becoming a string
    (e.g. a formatted '62.3%') is caught independent of the numeric range
    checks above."""
    bad = dict(_VALID_FIXTURE, macd_hist="1.85")
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "a string-typed macd_hist must fail the pinned schema"


def test_a_negative_price_level_fails_schema():
    """ma_50/ma_200/high_52w/low_52w are price levels; a units regression
    that turns one into a return/ratio (which can go negative) is caught."""
    bad = dict(_VALID_FIXTURE, ma_50=-1.0)
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "a negative ma_50 must fail the pinned schema"
