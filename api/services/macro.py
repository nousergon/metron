"""Macro market context for the Macro page.

Reads the curated macro indicators from FRED (free, public — no alpha-engine coupling,
so it serves the public free tier cleanly) and renders each as latest value + change
vs the prior reading + a short recent history. Global market data, not tenant-scoped.

Honest degradation: with no FRED API key configured the snapshot is marked
unavailable WITH a reason; an indicator FRED can't return is simply absent — never a
fabricated value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from api.config import settings
from portfolio_analytics.macro import INDICATORS, MacroSource, fetch_macro_series

# Cap the history returned per indicator (most recent first) — enough for a sparkline,
# small enough to keep the payload lean.
_HISTORY_LIMIT = 24


@dataclass
class MacroPoint:
    obs_date: date
    value: float


@dataclass
class MacroIndicator:
    key: str
    label: str
    units: str
    latest_value: float
    latest_date: date
    prior_value: float | None
    change: float | None  # latest − prior (same units), None with only one observation
    history: list[MacroPoint] = field(default_factory=list)


@dataclass
class MacroSummary:
    available: bool
    reason: str | None = None
    as_of: date | None = None
    indicators: list[MacroIndicator] = field(default_factory=list)


def macro_snapshot(*, api_key: str | None = None, source: MacroSource | None = None) -> MacroSummary:
    """Latest macro indicator readings from FRED. ``api_key`` defaults to the configured
    ``FRED_API_KEY``; ``source`` is injectable for tests. Unavailable (with a reason)
    when no key is set or FRED returns nothing."""
    api_key = api_key if api_key is not None else settings.fred_api_key
    if not api_key:
        return MacroSummary(False, reason="Macro data needs a free FRED API key — set FRED_API_KEY to enable it.")

    series_by_key = fetch_macro_series(INDICATORS, api_key, source=source)
    indicators: list[MacroIndicator] = []
    for ind in INDICATORS:
        series = series_by_key.get(ind.key)
        if series is None or not series.observations:
            continue  # FRED couldn't return it — omitted, not fabricated
        obs = series.observations  # ascending by date
        latest = obs[-1]
        prior = obs[-2] if len(obs) >= 2 else None
        recent = list(reversed(obs[-_HISTORY_LIMIT:]))  # most recent first
        indicators.append(
            MacroIndicator(
                key=ind.key,
                label=ind.label,
                units=ind.units,
                latest_value=latest.value,
                latest_date=latest.obs_date,
                prior_value=prior.value if prior else None,
                change=(latest.value - prior.value) if prior else None,
                history=[MacroPoint(obs_date=o.obs_date, value=o.value) for o in recent],
            )
        )

    if not indicators:
        return MacroSummary(False, reason="FRED returned no data — check the API key or try again shortly.")
    as_of = max(i.latest_date for i in indicators)
    return MacroSummary(True, as_of=as_of, indicators=indicators)
