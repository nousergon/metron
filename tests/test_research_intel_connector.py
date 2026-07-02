"""research_intel connector — normalize + fail-soft posture (config#1499 Phase 1)."""

from __future__ import annotations

import pytest

from portfolio_analytics.ingestion.research_intel_connector import (
    ResearchIntelConnector,
    ResearchIntelSnapshot,
)


def _artifact() -> dict:
    """A well-formed artifact against research_intel.schema.json v1."""
    return {
        "schema_version": 1,
        "date": "2026-07-04",
        "generated_at": "2026-07-04T13:00:00Z",
        "market_regime": "bull",
        "regime_narrative": "Broadening participation, easing financial conditions.",
        "sector_ratings": {
            "Technology": {"rating": "overweight", "rationale": "AI capex cycle."},
            "Utilities": {"rating": "underweight", "rationale": None},
        },
        "sector_modifiers": {"Technology": 1.15, "Utilities": 0.9},
        "market_breadth": {
            "pct_above_50d_ma": 62.5,
            "pct_above_200d_ma": 71.0,
            "advance_decline_ratio": 1.8,
        },
        "attractiveness": {
            "AAPL": {
                "ticker": "AAPL",
                "score": 78.0,
                "sector": "Technology",
                "breakdown": {"quant_score": 70.0, "qual_score": 82.0, "macro_shift": 3.0},
                "thesis": {"bull_case": "Services margin expansion.", "sector": "Technology"},
            },
            "XOM": {"ticker": "XOM", "score": 41.5, "sector": "Energy"},
        },
    }


def test_normalizes_a_wellformed_artifact():
    snap = ResearchIntelSnapshot.from_artifact(_artifact())
    assert not snap.is_empty
    assert snap.schema_version == 1
    assert snap.date == "2026-07-04"
    assert snap.market_regime == "bull"
    assert snap.sector_ratings["Technology"].rating == "overweight"
    assert snap.sector_ratings["Utilities"].rationale is None
    assert snap.sector_modifiers["Technology"] == pytest.approx(1.15)
    assert snap.market_breadth.pct_above_50d_ma == pytest.approx(62.5)
    aapl = snap.attractiveness["AAPL"]
    assert aapl.score == pytest.approx(78.0)
    assert aapl.breakdown.quant_score == pytest.approx(70.0)
    assert aapl.thesis.bull_case == "Services margin expansion."
    # XOM has no breakdown/thesis in the artifact → None, not fabricated.
    assert snap.attractiveness["XOM"].breakdown is None
    assert snap.attractiveness["XOM"].thesis is None


def test_dict_roundtrip_is_stable():
    snap = ResearchIntelSnapshot.from_artifact(_artifact())
    again = ResearchIntelSnapshot.from_dict(snap.to_dict())
    assert again.to_dict() == snap.to_dict()


def test_for_tickers_filters_case_insensitively():
    snap = ResearchIntelSnapshot.from_artifact(_artifact())
    subset = snap.for_tickers(["aapl", "  msft "])  # MSFT absent → simply not present
    assert set(subset) == {"AAPL"}
    assert set(snap.for_tickers(None)) == {"AAPL", "XOM"}


def test_out_of_contract_enums_and_bad_rows_are_dropped_not_trusted():
    art = _artifact()
    art["market_regime"] = "caution"  # retired 4-class label → nulled, not trusted
    art["sector_ratings"]["Technology"]["rating"] = "strong_buy"  # not in enum → nulled
    art["attractiveness"]["BROKEN"] = ["not", "a", "dict"]  # malformed row → dropped
    art["attractiveness"]["NUM"] = {"score": "not-a-number"}  # unparseable score → None
    snap = ResearchIntelSnapshot.from_artifact(art)
    assert snap.market_regime is None
    assert snap.sector_ratings["Technology"].rating is None
    assert "BROKEN" not in snap.attractiveness
    # a row keyed only by the dict key (no inner ticker) still resolves its symbol
    assert snap.attractiveness["NUM"].ticker == "NUM"
    assert snap.attractiveness["NUM"].score is None


def test_empty_artifact_is_empty():
    assert ResearchIntelSnapshot.from_artifact({}).is_empty


# ── connector fail-soft ──────────────────────────────────────────────────────
def test_connector_returns_snapshot_from_injected_reader():
    conn = ResearchIntelConnector(reader=_artifact)
    snap = conn.sync()
    assert snap.error is None
    assert snap.market_regime == "bull"


def test_connector_fail_soft_on_none_artifact():
    conn = ResearchIntelConnector(reader=lambda: None)
    snap = conn.sync()
    assert snap.error == "research_intel artifact unavailable"
    assert snap.is_empty


def test_connector_never_raises_on_reader_exception():
    def _boom():
        raise RuntimeError("s3 down")

    snap = ResearchIntelConnector(reader=_boom).sync()  # must not raise
    assert snap.error is not None
    assert "s3 down" in snap.error
    assert snap.is_empty
