"""Sector-vocabulary canonicalization (metron-ops#93).

``securities.sector`` had drifted across two taxonomies — yfinance Title-Case
("Technology"/"Healthcare") vs GICS-proper ("Information Technology"/"Health Care") —
so the same GICS sector split into two rows in the Allocation "By sector" breakdown.
The fix folds every label through ``canonical_sector`` at the source seam
(``fetch_sectors`` / ``fetch_benchmark_sector_weights``) so the one canonical taxonomy
(the ``SECTOR_ETF`` keys) reaches persistence and grouping. This pins the full
yfinance→GICS fold, idempotency on already-canonical values, and the seam wiring.
"""

from __future__ import annotations

import pytest

from portfolio_analytics.sectors import (
    SECTOR_ETF,
    canonical_sector,
    fetch_benchmark_sector_weights,
    fetch_sectors,
)

# (drifted input, expected canonical label). Covers the full GICS-proper → yfinance set.
DRIFT_CASES = [
    ("Information Technology", "Technology"),
    ("Health Care", "Healthcare"),
    ("Financials", "Financial Services"),
    ("Financial", "Financial Services"),
    ("Materials", "Basic Materials"),
    ("Consumer Discretionary", "Consumer Cyclical"),
    ("Consumer Staples", "Consumer Defensive"),
    ("Telecommunication Services", "Communication Services"),
    ("Telecommunications", "Communication Services"),
    ("Tech", "Technology"),
    ("Information_Technology", "Technology"),
]


class TestCanonicalSector:
    @pytest.mark.parametrize("raw, expected", DRIFT_CASES)
    def test_drift_variants_fold_to_canonical(self, raw, expected):
        assert canonical_sector(raw) == expected
        # ...and the canonical label is itself a real SECTOR_ETF key (never a typo).
        assert expected in SECTOR_ETF

    @pytest.mark.parametrize("canonical", list(SECTOR_ETF))
    def test_idempotent_on_canonical_values(self, canonical):
        # Already-canonical labels pass through unchanged, and re-folding is a no-op.
        assert canonical_sector(canonical) == canonical
        assert canonical_sector(canonical_sector(canonical)) == canonical

    @pytest.mark.parametrize("raw, expected", DRIFT_CASES)
    def test_fold_is_idempotent(self, raw, expected):
        # Folding a drift variant twice equals folding it once (stable fixed point).
        assert canonical_sector(canonical_sector(raw)) == expected

    def test_case_and_whitespace_insensitive(self):
        assert canonical_sector("  information technology  ") == "Technology"
        assert canonical_sector("HEALTH CARE") == "Healthcare"
        assert canonical_sector("technology") == "Technology"

    def test_none_and_empty_return_none(self):
        assert canonical_sector(None) is None
        assert canonical_sector("") is None
        assert canonical_sector("   ") is None

    def test_unknown_label_passes_through_trimmed(self):
        # Custom / unrecognized labels are not guessed — only trimmed (e.g. index ETFs).
        assert canonical_sector("Broad Market / Index") == "Broad Market / Index"
        assert canonical_sector("  Broad Market / Index ") == "Broad Market / Index"


class TestFetchSectorsNormalizes:
    def test_fetch_sectors_folds_drift_at_the_seam(self):
        # A source emitting GICS-proper labels collapses to one canonical taxonomy.
        source = lambda syms: {  # noqa: E731
            "MSFT": "Information Technology",
            "AAPL": "Technology",
            "JNJ": "Health Care",
            "JPM": "Financials",
        }
        out = fetch_sectors(["MSFT", "AAPL", "JNJ", "JPM"], source=source)
        assert out == {
            "MSFT": "Technology",
            "AAPL": "Technology",
            "JNJ": "Healthcare",
            "JPM": "Financial Services",
        }

    def test_fetch_sectors_drops_blank_after_fold(self):
        source = lambda syms: {"X": "Technology", "Y": "   ", "Z": ""}  # noqa: E731
        assert fetch_sectors(["X", "Y", "Z"], source=source) == {"X": "Technology"}


class TestBenchmarkWeightsNormalize:
    def test_benchmark_weights_fold_and_sum_collapsed_variants(self):
        # Two drift variants of the same sector collapse onto one canonical key,
        # and their fractions are summed (no double-counted / split benchmark row).
        source = lambda: {  # noqa: E731
            "Technology": 0.20,
            "Information Technology": 0.10,
            "Health Care": 0.13,
        }
        out = fetch_benchmark_sector_weights(source=source)
        assert out == {"Technology": pytest.approx(0.30), "Healthcare": pytest.approx(0.13)}
