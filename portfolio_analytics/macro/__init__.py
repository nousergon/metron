"""Macro indicator reference data + sourcing for the Macro page.

The curated set of macro indicators (FRED series id → canonical label + units) and an
injectable source seam that returns each indicator's recent observation series. The
default reads FRED directly (free, public — correct for the personal AND the public
free tier, no alpha-engine coupling); tests inject deterministic series.

Every indicator is a self-explanatory rate or level (no derived YoY arithmetic), so a
"latest value + change vs prior reading" reads honestly without hidden transforms.
"""

from portfolio_analytics.macro.fred_source import (
    INDICATORS,
    MacroObservation,
    MacroSeries,
    MacroSource,
    fetch_macro_series,
)

__all__ = ["INDICATORS", "MacroObservation", "MacroSeries", "MacroSource", "fetch_macro_series"]
