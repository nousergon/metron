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

from portfolio_analytics.macro import INDICATORS, MacroSource, fetch_macro_series

# Cap the history returned per indicator (most recent first). The Overview strip needs
# only latest + change, so it stays lean at the default; the Macro detail page requests a
# deeper window (~1y of daily series) for its charts via ``history_limit``.
_HISTORY_LIMIT = 24
# Deep window for the Macro detail page — ~1y+ of a daily series (e.g. DGS10/VIX); a
# monthly series simply returns all it has within the producer's ~2y artifact.
FULL_HISTORY_LIMIT = 400


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


def macro_snapshot(*, source: MacroSource | None = None, history_limit: int = _HISTORY_LIMIT) -> MacroSummary:
    """Latest macro indicator readings from the data spine (`alpha-engine-data`'s macro
    artifact). ``source`` is injectable for tests; ``history_limit`` caps the per-indicator
    history (most recent first) — small for the Overview strip, ``FULL_HISTORY_LIMIT`` for
    the Macro detail-page charts. Unavailable (with a reason) when the spine hasn't
    published macro indicators yet."""
    series_by_key = fetch_macro_series(INDICATORS, source=source)
    if not series_by_key:
        return MacroSummary(False, reason="Macro data unavailable — the data spine has no macro indicators yet.")
    indicators: list[MacroIndicator] = []
    for ind in INDICATORS:
        series = series_by_key.get(ind.key)
        if series is None or not series.observations:
            continue  # FRED couldn't return it — omitted, not fabricated
        obs = series.observations  # ascending by date
        latest = obs[-1]
        prior = obs[-2] if len(obs) >= 2 else None
        recent = list(reversed(obs[-history_limit:]))  # most recent first
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
