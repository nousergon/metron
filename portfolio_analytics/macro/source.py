"""Curated macro indicators + the macro source seam.

A small, deliberately self-explanatory indicator set — each a rate or level published
directly (no YoY/MoM arithmetic to misread) — fetched through an injectable source. The
DEFAULT is the **data spine**: Metron reads macro indicators from `alpha-engine-data`'s
S3 artifact and makes no direct FRED call (imported lazily, so importing this module
needs no boto3/network). Fail-soft per indicator: a series the source can't return is
omitted, never fabricated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Indicator:
    """One macro series: its FRED id, a canonical display label, and units."""

    key: str
    series_id: str
    label: str
    units: str  # "%" | "index"


# Each is a level/rate as published — "latest + change vs prior reading" needs no
# transform. Order is the display order on the Macro page.
INDICATORS: list[Indicator] = [
    Indicator("fed_funds", "FEDFUNDS", "Fed funds rate", "%"),
    Indicator("unemployment", "UNRATE", "Unemployment", "%"),
    Indicator("cpi_inflation", "T10YIE", "10Y breakeven inflation", "%"),
    Indicator("ust_10y", "DGS10", "10Y Treasury", "%"),
    Indicator("ust_2y", "DGS2", "2Y Treasury", "%"),
    Indicator("yield_curve", "T10Y2Y", "10Y–2Y curve", "%"),
    Indicator("vix", "VIXCLS", "VIX", "index"),
]


@dataclass(frozen=True)
class MacroObservation:
    obs_date: date
    value: float


@dataclass
class MacroSeries:
    """One indicator's recent observations, ascending by date (most recent last)."""

    observations: list[MacroObservation] = field(default_factory=list)


# A macro source maps the indicator set → each indicator key's recent series. The second
# positional arg is a (now-optional) API key, kept for source-signature compatibility —
# the default data-spine source ignores it (the producer already fetched FRED).
MacroSource = Callable[[list[Indicator], str], dict[str, MacroSeries]]


def fetch_macro_series(
    indicators: list[Indicator], api_key: str = "", *, source: MacroSource | None = None
) -> dict[str, MacroSeries]:
    """Recent observation series per indicator key. Deduped indicator set.

    Returns ``{}`` for an empty indicator set. An indicator the source can't fetch is
    omitted from the result (the caller surfaces it as missing, never fabricated)."""
    if not indicators:
        return {}
    if source is None:
        from portfolio_analytics.macro.spine_source import spine_macro_series
        source = spine_macro_series
    return source(indicators, api_key)
