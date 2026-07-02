"""research_intel last-good store — persist/round-trip + fail-soft (config#1499 Phase 1)."""

from __future__ import annotations

from portfolio_analytics.ingestion import research_intel_store as store
from portfolio_analytics.ingestion.research_intel_connector import (
    ResearchIntelConnector,
    ResearchIntelSnapshot,
)


def _snapshot() -> ResearchIntelSnapshot:
    return ResearchIntelSnapshot.from_artifact(
        {
            "schema_version": 1,
            "date": "2026-07-04",
            "market_regime": "neutral",
            "sector_ratings": {"Energy": {"rating": "market_weight"}},
            "attractiveness": {"XOM": {"ticker": "XOM", "score": 55.0}},
        }
    )


def test_save_then_load_roundtrips(tmp_path):
    path = tmp_path / "research_intel.json"
    assert store.save_research_intel(_snapshot(), path=path) is True
    loaded = store.load_research_intel(path=path)
    assert loaded is not None
    assert loaded.market_regime == "neutral"
    assert loaded.attractiveness["XOM"].score == 55.0


def test_load_missing_returns_none(tmp_path):
    assert store.load_research_intel(path=tmp_path / "absent.json") is None


def test_load_corrupt_returns_none(tmp_path):
    path = tmp_path / "research_intel.json"
    path.write_text("{ this is not json")
    assert store.load_research_intel(path=path) is None


def test_error_snapshot_is_not_persisted_keeps_last_good(tmp_path):
    path = tmp_path / "research_intel.json"
    store.save_research_intel(_snapshot(), path=path)  # last-good
    # A subsequent failed fetch must NOT clobber the last-good.
    assert store.save_research_intel(ResearchIntelSnapshot(error="s3 down"), path=path) is False
    assert store.load_research_intel(path=path).market_regime == "neutral"


def test_sync_persists_from_connector(tmp_path):
    path = tmp_path / "research_intel.json"
    conn = ResearchIntelConnector(reader=lambda: _snapshot().to_dict())
    assert store.sync_research_intel(conn, path=path) is True
    assert store.load_research_intel(path=path) is not None


def test_sync_fail_soft_keeps_last_good(tmp_path):
    path = tmp_path / "research_intel.json"
    store.sync_research_intel(ResearchIntelConnector(reader=lambda: _snapshot().to_dict()), path=path)
    bad = ResearchIntelConnector(reader=lambda: None)
    assert store.sync_research_intel(bad, path=path) is False
    assert store.load_research_intel(path=path).market_regime == "neutral"
