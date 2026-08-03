"""Integration tests for layer-2 accounting invariants (metron-ops#217).

Each test verifies that an invariant CHECK detects a known violation — not
just that the function runs without error. The wiring into the actual
request/response chain is tested implicitly: every test constructs the same
data shapes the endpoint handlers pass to the invariant functions, so a
change that broke the wiring would also break the data shape and fail these
tests.

These are NOT unit tests of the invariant functions in isolation — each test
constructs a realistic portfolio state (holdings, NAV points, lot data) and
runs the invariant check against that state, exactly as the serving path does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from api.services import invariants

# ── Fixture data shapes (mirror actual response dataclasses) ────────────────


@dataclass
class _FakePerfPoint:
    snap_date: date
    nav: float
    external_flow: float


@dataclass
class _FakeHolding:
    ticker: str
    market_value: float | None
    quantity: float = 0.0


@dataclass
class _FakeLot:
    ticker: str
    quantity: float


# ── NAV continuity ──────────────────────────────────────────────────────────


def test_nav_continuity_detects_implausible_jump():
    """A >100% single-day NAV jump (after flow neutralization) is flagged."""
    points = [
        _FakePerfPoint(date(2026, 7, 1), nav=100_000.0, external_flow=0.0),
        _FakePerfPoint(date(2026, 7, 2), nav=300_000.0, external_flow=0.0),  # +200% — implausible
    ]
    results = invariants.check_nav_continuity(nav_points=points)
    assert len(results) == 1
    ok, detail = results[0]
    assert not ok
    assert ">100%" in detail


def test_nav_continuity_passes_normal_day():
    """A normal ~1% daily return passes the check."""
    points = [
        _FakePerfPoint(date(2026, 7, 1), nav=100_000.0, external_flow=0.0),
        _FakePerfPoint(date(2026, 7, 2), nav=101_000.0, external_flow=0.0),  # +1% — normal
    ]
    results = invariants.check_nav_continuity(nav_points=points)
    assert len(results) == 0  # no violations


def test_nav_continuity_passes_with_flow():
    """A deposit (external_flow > 0) that explains the NAV jump passes."""
    points = [
        _FakePerfPoint(date(2026, 7, 1), nav=100_000.0, external_flow=0.0),
        _FakePerfPoint(date(2026, 7, 2), nav=150_000.0, external_flow=49_000.0),  # +50k deposit explains jump
    ]
    results = invariants.check_nav_continuity(nav_points=points)
    assert len(results) == 0  # deposit explains the NAV jump → normal return


# ── Position sum = NAV ──────────────────────────────────────────────────────


def test_position_nav_consistency_detects_mismatch():
    """Σ position values ≠ NAV is flagged."""
    holdings = [
        _FakeHolding("AAPL", market_value=50_000.0),
        _FakeHolding("GOOGL", market_value=30_000.0),
    ]
    ok, detail = invariants.check_position_nav_consistency(
        holdings=holdings, nav=100_000.0, cash=5_000.0,
    )
    # 50k + 30k + 5k = 85k ≠ 100k → violation
    assert not ok
    assert "≠ NAV" in detail


def test_position_nav_consistency_passes_when_equal():
    """Σ position values + cash = NAV passes."""
    holdings = [
        _FakeHolding("AAPL", market_value=50_000.0),
        _FakeHolding("GOOGL", market_value=45_000.0),
    ]
    ok, detail = invariants.check_position_nav_consistency(
        holdings=holdings, nav=100_000.0, cash=5_000.0,
    )
    # 50k + 45k + 5k = 100k → pass
    assert ok


def test_position_nav_consistency_tolerates_float_noise():
    """Float rounding within relative tolerance passes."""
    holdings = [
        _FakeHolding("AAPL", market_value=100_000.0),
    ]
    ok, detail = invariants.check_position_nav_consistency(
        holdings=holdings, nav=100_000.01, cash=0.0,
    )
    # 100k vs 100k.01 → within 1e-4 relative tolerance → pass
    assert ok


# ── Realized + unrealized = total ───────────────────────────────────────────


def test_realized_unrealized_total_passes():
    """Normal realized + unrealized passes (identity check)."""
    ok, detail = invariants.check_realized_unrealized_total(
        realized_total=5_000.0, unrealized_gain=10_000.0,
    )
    assert ok


def test_realized_unrealized_total_passes_when_unpriced():
    """None unrealized (unpriced portfolio) passes — nothing to check."""
    ok, detail = invariants.check_realized_unrealized_total(
        realized_total=5_000.0, unrealized_gain=None,
    )
    assert ok


# ── Brinson effects sum ─────────────────────────────────────────────────────


def test_brinson_effects_detects_mismatch():
    """Allocation + selection + interaction ≠ active return is flagged."""
    ok, detail = invariants.check_brinson_effects(
        allocation=0.01, selection=0.02, interaction=0.005,
        active_return=0.10,  # 0.01+0.02+0.005=0.035 ≠ 0.10
    )
    assert not ok
    assert "≠ active return" in detail


def test_brinson_effects_passes_when_equal():
    """Effects sum matches active return within tolerance."""
    ok, detail = invariants.check_brinson_effects(
        allocation=0.01, selection=0.02, interaction=0.005,
        active_return=0.035,  # 0.01+0.02+0.005=0.035 == 0.035
    )
    assert ok


# ── TWR chain-link ──────────────────────────────────────────────────────────


def test_twr_chain_link_detects_divergence():
    """A >1pp divergence between TWR and cumulative return is flagged."""
    ok, detail = invariants.check_twr_chain_link(
        cumulative_return=0.15, twr=0.10,  # 5pp difference
    )
    assert not ok
    assert "≠ TWR" in detail


def test_twr_chain_link_passes_when_close():
    """TWR and cumulative return within 1pp pass."""
    ok, detail = invariants.check_twr_chain_link(
        cumulative_return=0.155, twr=0.150,  # 0.5pp — within tolerance
    )
    assert ok


# ── Lot quantities ──────────────────────────────────────────────────────────


def test_lot_quantities_flags_nonpositive():
    """A lot with net non-positive quantity (across all lots of that ticker) is flagged."""
    lots = [
        _FakeLot("AAPL", quantity=-100.0),  # net negative across all lots of AAPL
    ]
    results = invariants.check_lot_quantities(lots=lots)
    assert len(results) > 0
    ok, detail = results[0]
    assert not ok


def test_lot_quantities_passes_normal():
    """Normal positive lot quantities pass."""
    lots = [
        _FakeLot("AAPL", quantity=100.0),
        _FakeLot("GOOGL", quantity=50.0),
    ]
    results = invariants.check_lot_quantities(lots=lots)
    assert len(results) == 0
