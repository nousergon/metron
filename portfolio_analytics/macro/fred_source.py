"""Curated macro indicators + the FRED-backed source seam.

A small, deliberately self-explanatory indicator set — each a rate or level FRED
publishes directly (no YoY/MoM arithmetic to misread) — fetched through an injectable
source so the free FRED default can be swapped (or mocked in tests). Fail-soft per
indicator: a series FRED can't return is omitted, never fabricated.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date

logger = logging.getLogger(__name__)


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


# A macro source maps the indicator set → each indicator key's recent series, given an
# API key. The default hits FRED; tests + any future feed inject their own.
MacroSource = Callable[[list[Indicator], str], dict[str, MacroSeries]]

_FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch_macro_series(
    indicators: list[Indicator], api_key: str, *, source: MacroSource | None = None
) -> dict[str, MacroSeries]:
    """Recent observation series per indicator key. Deduped indicator set.

    Returns ``{}`` for an empty indicator set. An indicator the source can't fetch is
    omitted from the result (the caller surfaces it as missing, never fabricated)."""
    if not indicators or not api_key:
        return {}
    source = source or _fred_observations
    return source(indicators, api_key)


def _fred_observations(indicators: list[Indicator], api_key: str) -> dict[str, MacroSeries]:  # pragma: no cover - network
    """Default source: one FRED ``series/observations`` GET per indicator (~last 2y).

    Fail-soft per indicator (network / schema / a "." missing-value marker) — a series
    that fails is omitted. stdlib urllib only (no new dependency). Excluded from unit
    coverage; exercised live, mirroring the price/sector sources."""
    out: dict[str, MacroSeries] = {}
    for ind in indicators:
        params = urllib.parse.urlencode(
            {
                "series_id": ind.series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "asc",
                "observation_start": _two_years_ago_iso(),
            }
        )
        try:
            with urllib.request.urlopen(f"{_FRED_OBS_URL}?{params}", timeout=15) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as e:  # network / parse
            logger.warning("FRED fetch failed for %s: %s", ind.series_id, e)
            continue
        obs: list[MacroObservation] = []
        for row in payload.get("observations", []):
            raw = row.get("value")
            if raw in (None, "", "."):  # FRED uses "." for a missing reading
                continue
            try:
                obs.append(MacroObservation(obs_date=date.fromisoformat(row["date"]), value=float(raw)))
            except (ValueError, KeyError):
                continue
        if obs:
            out[ind.key] = MacroSeries(observations=obs)
    return out


def _two_years_ago_iso() -> str:  # pragma: no cover - trivial date math, network path only
    from datetime import datetime

    today = datetime.now(UTC).date()
    try:
        return today.replace(year=today.year - 2).isoformat()
    except ValueError:  # Feb 29
        return today.replace(year=today.year - 2, day=28).isoformat()
