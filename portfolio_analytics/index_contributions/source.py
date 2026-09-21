"""Index constituent weight x return decomposition — the index-contributions source seam.

Mirrors ``portfolio_analytics.prices.source`` and ``portfolio_analytics.sectors.source``:
an injectable callable, defaulting to a **data-spine** read, so Metron never makes a
direct market-data call for the name-level index decomposition either. The artifact is
produced by the collector (``alpha-engine-config-I11297``) — Metron reads it, never
computes membership/weights/per-name returns itself.

**Unit boundary (read this before touching any number here):** the artifact mixes
percentage-point and return-fraction fields, and getting the mix wrong is the likeliest
defect in the whole feature.

  - ``IndexConstituent.weight_prior_close`` — already a fraction of the index (0.0121 =
    1.21%). Used as-is.
  - ``IndexConstituent.return_pct`` — despite the name, already a return FRACTION
    (0.288 = +28.8%), consistent with how ``nousergon_lib.quant.attribution`` treats
    every return. Used as-is, never divided by 100. (Verified against the worked
    example in the producer contract: 0.0121 weight x 0.288 return = 0.00348512, which
    matches ``contribution_pp`` of 0.348 only once ``contribution_pp`` is read as
    PERCENTAGE POINTS — i.e. divided by 100. If ``return_pct`` were also percentage
    points the two would disagree by two orders of magnitude.)
  - ``IndexConstituent.contribution_pp`` and the artifact-level ``index_return_pct`` /
    ``residual_pp`` — genuinely in PERCENTAGE POINTS. Divide by 100 for a fraction
    (see the ``*_fraction`` properties below). These are never fed into
    ``nousergon_lib``'s per-security math directly — they exist for cross-checking and
    display; the fraction properties do that conversion once, at the boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexConstituent:
    symbol: str
    weight_prior_close: float  # fraction of the index, as published
    return_pct: float  # a return FRACTION despite the name — see module docstring
    contribution_pp: float  # PERCENTAGE POINTS — see ``contribution_fraction``

    @property
    def contribution_fraction(self) -> float:
        return self.contribution_pp / 100.0


@dataclass(frozen=True)
class IndexContributionsArtifact:
    schema_version: int
    index: str
    proxy_symbol: str
    as_of: date
    prior_close_date: date
    index_return_pct: float  # PERCENTAGE POINTS — see ``index_return_fraction``
    weight_method: str
    residual_pp: float  # PERCENTAGE POINTS — see ``residual_fraction``
    coverage_weight_with_return: float
    coverage_members: int
    coverage_members_missing_return: int
    constituents: list[IndexConstituent] = field(default_factory=list)

    @property
    def index_return_fraction(self) -> float:
        return self.index_return_pct / 100.0

    @property
    def residual_fraction(self) -> float:
        return self.residual_pp / 100.0


# A source maps (index, as_of) to the raw artifact dict (or None when absent for that
# date). Default = the data spine; tests inject fixtures.
IndexContributionsSource = Callable[[str, date], dict | None]


def _parse(raw: dict) -> IndexContributionsArtifact | None:
    """Parse the raw artifact dict, applying the unit-boundary conversions described in
    the module docstring. Malformed input degrades to None (fail-soft, like every other
    spine reader) rather than raising into a request handler."""
    try:
        coverage = raw.get("coverage") or {}
        constituents = [
            IndexConstituent(
                symbol=str(c["symbol"]),
                weight_prior_close=float(c["weight_prior_close"]),
                return_pct=float(c["return_pct"]),
                contribution_pp=float(c["contribution_pp"]),
            )
            for c in raw.get("constituents", [])
        ]
        return IndexContributionsArtifact(
            schema_version=int(raw["schema_version"]),
            index=str(raw["index"]),
            proxy_symbol=str(raw["proxy_symbol"]),
            as_of=date.fromisoformat(raw["as_of"]),
            prior_close_date=date.fromisoformat(raw["prior_close_date"]),
            index_return_pct=float(raw["index_return_pct"]),
            weight_method=str(raw.get("weight_method", "unknown")),
            residual_pp=float(raw.get("residual_pp", 0.0)),
            coverage_weight_with_return=float(coverage.get("weight_with_return", 0.0)),
            coverage_members=int(coverage.get("members", 0)),
            coverage_members_missing_return=int(coverage.get("members_missing_return", 0)),
            constituents=constituents,
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("index-contributions artifact malformed for %r: %s", raw.get("index"), e)
        return None


def fetch_index_contributions(
    index: str, as_of: date, *, source: IndexContributionsSource | None = None
) -> IndexContributionsArtifact | None:
    """The parsed index-contributions artifact for ``index`` on ``as_of``, or None when
    unavailable/malformed (never fabricated — the caller renders "unexplained", not "no
    drivers"). ``source`` defaults to the data spine (imported lazily so importing this
    module needs no boto3/network)."""
    if source is None:
        from portfolio_analytics.index_contributions.spine_source import spine_index_contributions
        source = spine_index_contributions
    raw = source(index, as_of)
    if not raw:
        return None
    art = _parse(raw)
    if art is not None and art.as_of != as_of:
        # Defensive: a source (e.g. a stale `latest.json` fallback) returning a
        # different day's decomposition than asked for is a mismatch, not a hit — never
        # silently substitute one day's story for another's.
        logger.warning("index-contributions artifact for %s dated %s, expected %s", index, art.as_of, as_of)
        return None
    return art
