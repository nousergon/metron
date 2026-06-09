"""Stress testing — portfolio impact under named scenarios.

Pure stdlib, data-source-agnostic. Two complementary scenario styles:

  - **historical replay** — apply each holding's *actual* return over a named past
    crisis window (e.g. the 2020 COVID crash) to today's weights. Fully sourced
    from real prices; the honest "what would this exact portfolio have done then".
  - **factor shock** — a hypothetical move expressed in the factor space (e.g.
    "broad market −10%"), translated through the portfolio's factor exposures
    (``Bᵀw`` from the VA5 model). Forward-looking and counterfactual.

Both return a portfolio return as a signed fraction (negative = loss). Risk
*measurement* — descriptive, no advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class HistoricalScenario:
    """A named historical stress window (inclusive of both endpoints)."""

    name: str
    start: date
    end: date
    description: str = ""


# Canonical equity stress windows (peak→trough of well-known drawdowns). Returns
# are sourced live from the price cache, so these are just date ranges.
HISTORICAL_SCENARIOS: list[HistoricalScenario] = [
    HistoricalScenario("COVID crash", date(2020, 2, 19), date(2020, 3, 23), "Fastest 30%+ S&P drawdown on record"),
    HistoricalScenario("2022 bear market", date(2022, 1, 3), date(2022, 10, 12), "Rate-hike / inflation drawdown"),
    HistoricalScenario("2018 Q4 selloff", date(2018, 9, 20), date(2018, 12, 24), "Rate-fears / growth-scare quarter"),
    HistoricalScenario("Aug 2024 unwind", date(2024, 7, 16), date(2024, 8, 5), "Yen carry-trade / volatility spike"),
]


@dataclass(frozen=True)
class FactorShock:
    """A named hypothetical, as a map of factor label → shock return."""

    name: str
    shocks: dict[str, float]
    description: str = ""


# Hypothetical shocks in the VA5 factor space (Market + style spreads). "Market"
# moves the whole book through its market beta; spread shocks tilt a style.
FACTOR_SHOCKS: list[FactorShock] = [
    FactorShock("Market −10%", {"Market": -0.10}, "Broad equity selloff"),
    FactorShock("Market −20%", {"Market": -0.20}, "Severe bear leg"),
    FactorShock("Momentum reversal −10%", {"Momentum": -0.10}, "Momentum factor unwinds"),
    FactorShock("Flight to quality", {"Market": -0.08, "Quality": 0.04, "Low Volatility": 0.04}, "Risk-off rotation"),
]


def factor_shock_impact(exposures: dict[str, float], shocks: dict[str, float]) -> float:
    """Portfolio return under a factor shock: ``Σ exposure_f · shock_f``.

    Factors absent from ``shocks`` are unshocked (0). Idiosyncratic moves are
    assumed 0 in a factor scenario (a diversified book's stock-specific shocks wash
    out), so this is the systematic impact only.
    """
    return sum(exposures.get(f, 0.0) * s for f, s in shocks.items())


def historical_scenario_impact(
    weights: dict[str, float],
    holding_returns: dict[str, float],
) -> tuple[float, float]:
    """Weighted portfolio return over a window + the weight fraction covered.

    ``weights`` are raw (e.g. market values); only holdings present in
    ``holding_returns`` contribute, and their weights are renormalized over that
    covered set. Returns ``(impact, coverage)`` where ``coverage`` is covered
    weight / total weight — so a partly-uncovered scenario is surfaced, not hidden.
    Coverage 0 (nothing priced) yields ``(0.0, 0.0)``.
    """
    total = sum(weights.values())
    covered = {t: w for t, w in weights.items() if t in holding_returns}
    covered_w = sum(covered.values())
    if covered_w <= 0:
        return 0.0, 0.0
    impact = sum(w / covered_w * holding_returns[t] for t, w in covered.items())
    coverage = covered_w / total if total > 0 else 0.0
    return impact, coverage
