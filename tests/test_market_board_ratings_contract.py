"""Consumer contract test — market_board.py composes rows from the SAME pinned producer
schema ``test_technical_rating.py`` and ``test_deploy_cash.py`` already exercise
(``tests/contracts/technical_ratings.schema.json``, metron-ops#294 / P-07 precedent):
Metron has no importable dependency on the nousergon-data producer, so the versioned pinned
schema IS the coupling. This pins the SAME boundary specifically for the market-board
consumer, so a drift in that schema is caught here even if a sibling test file changes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema

from api.routers import market_board
from api.services import technical_rating

CONTRACTS_DIR = Path(__file__).parent / "contracts"
SCHEMA_PATH = CONTRACTS_DIR / "technical_ratings.schema.json"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _fixture_artifact() -> dict:
    return {
        "schema_version": 1,
        "as_of_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "ratings": {
            "AAPL": {
                "score": 0.55, "label": "Buy", "ma_score": 0.6, "osc_score": 0.5,
                "n_buy": 9, "n_neutral": 1, "n_sell": 1, "n_votes": 11,
            },
        },
    }


def test_the_pinned_schema_is_itself_valid_json_schema():
    jsonschema.Draft202012Validator.check_schema(_schema())


def test_a_schema_conformant_fixture_composes_into_a_market_board_row():
    fixture = _fixture_artifact()
    jsonschema.validate(instance=fixture, schema=_schema())

    def _reader():
        return fixture

    snapshot = technical_rating.load_technical_rating(intraday_reader=_reader)
    rating = snapshot.by_symbol["AAPL"]

    row = market_board.MarketBoardRowOut(
        symbol="AAPL", held=True, label=rating.label, score=rating.score,
        ma_score=rating.ma_score, osc_score=rating.osc_score,
        change_1d_pct=None, change_5d_pct=None, basis=rating.basis, as_of=rating.as_of,
    )
    assert row.label == "Buy"
    assert row.score == 0.55
    assert row.basis == "intraday"


def test_an_unknown_label_fails_the_pinned_schema():
    """The producer's label vocabulary is closed (RATING_LABELS) — a label outside it
    would be dropped by the real consumer, and a fixture using one must already fail the
    pinned schema, so this test can never silently start asserting on a dropped value."""
    bad = _fixture_artifact()
    bad["ratings"]["AAPL"]["label"] = "Hold"  # not in the producer's enum
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "an out-of-vocabulary label must fail the pinned schema"


def test_score_out_of_range_fails_the_pinned_schema():
    bad = _fixture_artifact()
    bad["ratings"]["AAPL"]["score"] = 1.5  # score is pinned to [-1, +1]
    errors = list(jsonschema.Draft202012Validator(_schema()).iter_errors(bad))
    assert errors, "a score outside [-1, 1] must fail the pinned schema"
