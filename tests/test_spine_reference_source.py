"""Data-spine sectors + earnings sources — Metron's consumer side of the contract
with `alpha-engine-data`. The producer writes `market_data/sectors/latest.json` +
`market_data/earnings/latest.json` (keyed by yf_symbol); this verifies Metron reads
them, fail-softs a missing artifact, and that the dispatchers default to the spine.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from portfolio_analytics.calendar import fetch_earnings_dates
from portfolio_analytics.calendar import spine_source as cal_spine
from portfolio_analytics.sectors import fetch_benchmark_sector_weights, fetch_sectors
from portfolio_analytics.sectors import spine_source as sec_spine


def _s3_with(objects: dict[str, dict | None]) -> MagicMock:
    s3 = MagicMock()

    def _get(*, Bucket, Key):  # noqa: N803
        if Key not in objects or objects[Key] is None:
            raise Exception(f"NoSuchKey: {Key}")
        body = MagicMock()
        body.read.return_value = json.dumps(objects[Key]).encode()
        return {"Body": body}

    s3.get_object.side_effect = _get
    return s3


_SECTORS = {"schema_version": 1, "as_of": "2026-06-11",
            "sectors": {"AAPL": "Technology", "1299.HK": "Financial Services"},
            "spy_sector_weights": {"Technology": 0.30, "Financial Services": 0.13}}
_EARNINGS = {"schema_version": 1, "as_of": "2026-06-11", "earnings": {"AAPL": "2026-07-30"}}


class TestSectors:
    def test_sectors_and_benchmark_weights_read_from_artifact(self):
        s3 = _s3_with({sec_spine.SECTORS_LATEST_KEY: _SECTORS})
        out = sec_spine.spine_sectors(["AAPL", "1299.HK", "NOTHELD"], s3=s3)
        assert out == {"AAPL": "Technology", "1299.HK": "Financial Services"}  # NOTHELD omitted
        weights = sec_spine.spine_benchmark_sector_weights(s3=s3)
        assert weights == {"Technology": 0.30, "Financial Services": 0.13}

    def test_missing_artifact_fail_soft(self):
        s3 = _s3_with({sec_spine.SECTORS_LATEST_KEY: None})
        assert sec_spine.spine_sectors(["AAPL"], s3=s3) == {}
        assert sec_spine.spine_benchmark_sector_weights(s3=s3) == {}

    def test_fetch_sectors_defaults_to_spine(self, monkeypatch):
        monkeypatch.setattr(sec_spine, "spine_sectors", lambda syms, **k: {"AAPL": "Technology"})
        assert fetch_sectors(["AAPL", "AAPL", ""]) == {"AAPL": "Technology"}

    def test_fetch_benchmark_defaults_to_spine(self, monkeypatch):
        monkeypatch.setattr(sec_spine, "spine_benchmark_sector_weights", lambda **k: {"Technology": 0.3})
        assert fetch_benchmark_sector_weights() == {"Technology": 0.3}


class TestEarnings:
    def test_earnings_dates_parsed_from_artifact(self):
        s3 = _s3_with({cal_spine.EARNINGS_LATEST_KEY: _EARNINGS})
        out = cal_spine.spine_earnings_dates(["AAPL", "1299.HK"], s3=s3)
        assert out == {"AAPL": date(2026, 7, 30)}  # 1299.HK undated → omitted

    def test_missing_artifact_fail_soft(self):
        s3 = _s3_with({cal_spine.EARNINGS_LATEST_KEY: None})
        assert cal_spine.spine_earnings_dates(["AAPL"], s3=s3) == {}

    def test_fetch_earnings_defaults_to_spine(self, monkeypatch):
        monkeypatch.setattr(cal_spine, "spine_earnings_dates", lambda syms, **k: {"AAPL": date(2026, 7, 30)})
        assert fetch_earnings_dates(["AAPL"]) == {"AAPL": date(2026, 7, 30)}
