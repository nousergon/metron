"""Tests for analytics/stress.py (pure scenario math)."""

import pytest

from portfolio_analytics.domain import stress


class TestFactorShockImpact:
    def test_weighted_sum_of_exposures(self):
        exposures = {"Market": 1.1, "Momentum": 0.3}
        # Market −10% through beta 1.1 = −11%; Momentum unshocked.
        assert stress.factor_shock_impact(exposures, {"Market": -0.10}) == pytest.approx(-0.11)

    def test_multi_factor_shock(self):
        exposures = {"Market": 1.0, "Quality": 0.5, "Low Volatility": -0.2}
        shocks = {"Market": -0.08, "Quality": 0.04, "Low Volatility": 0.04}
        # 1.0*-0.08 + 0.5*0.04 + -0.2*0.04 = -0.08 + 0.02 - 0.008
        assert stress.factor_shock_impact(exposures, shocks) == pytest.approx(-0.068)

    def test_unknown_factor_ignored(self):
        assert stress.factor_shock_impact({"Market": 1.0}, {"Nonexistent": -0.5}) == pytest.approx(0.0)


class TestHistoricalScenarioImpact:
    def test_renormalizes_over_covered(self):
        weights = {"AAPL": 6000.0, "MSFT": 4000.0}
        returns = {"AAPL": -0.30, "MSFT": -0.20}
        impact, coverage = stress.historical_scenario_impact(weights, returns)
        assert impact == pytest.approx(0.6 * -0.30 + 0.4 * -0.20)
        assert coverage == pytest.approx(1.0)

    def test_partial_coverage_renormalizes_and_reports(self):
        weights = {"AAPL": 7000.0, "OLD": 3000.0}
        returns = {"AAPL": -0.25}  # OLD has no return in the window
        impact, coverage = stress.historical_scenario_impact(weights, returns)
        assert impact == pytest.approx(-0.25)  # AAPL is 100% of covered weight
        assert coverage == pytest.approx(0.7)

    def test_zero_coverage(self):
        impact, coverage = stress.historical_scenario_impact({"AAPL": 1000.0}, {})
        assert (impact, coverage) == (0.0, 0.0)


class TestScenarioCatalogs:
    def test_historical_windows_well_ordered(self):
        for sc in stress.HISTORICAL_SCENARIOS:
            assert sc.start < sc.end

    def test_factor_shocks_nonempty(self):
        assert stress.FACTOR_SHOCKS
        for fs in stress.FACTOR_SHOCKS:
            assert fs.shocks
