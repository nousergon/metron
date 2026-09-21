"""Index constituent weight x return decomposition — the index-contributions source seam.

Mirrors ``portfolio_analytics.prices.source`` and ``portfolio_analytics.sectors.source``:
an injectable callable, defaulting to a **data-spine** read, so Metron never makes a
direct market-data call for the name-level index decomposition either. The artifact is
produced by the collector (``alpha-engine-config-I11297``) — Metron reads it, never
computes membership/weights/per-name returns itself.

**Unit boundary (read this before touching any number here — corrected metron-ops-I346,
2026-09-21; an earlier revision of the issue's worked example was internally
inconsistent and a first pass at this module reverse-engineered the wrong convention
from it):** ONE rule, no field-by-field exception. ``weight_prior_close`` is the ONLY
fraction in the artifact (0.0121 = 1.21% of the index). Every OTHER number — the
per-constituent ``return_pct``, ``contribution_pp``, and the artifact-level
``index_return_pct`` / ``residual_pp`` — is on the 100-scale (percent and percentage
points are the same numeric scale) and divides by 100 to reach a fraction. That
includes ``return_pct`` despite the name suggesting otherwise: it is PERCENT
(28.8 means +28.8%), not already a fraction.

The identity that pins this down: ``weight_prior_close * return_pct == contribution_pp``
— e.g. 0.0121 * 28.8 == 0.348. A fraction (weight) times a percent (return) yields a
percentage-point result directly, with no additional scaling on either side of that
multiplication; only the CONSUMER (``nousergon_lib.quant.attribution``, which works
entirely in fractions) needs the ``/100`` conversion, applied once here via the
``*_fraction`` properties below. Never feed a raw ``return_pct`` / ``contribution_pp`` /
``index_return_pct`` / ``residual_pp`` to that library — always go through the matching
``*_fraction`` property.
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
    weight_prior_close: float  # fraction of the index, as published — the ONLY fraction here
    return_pct: float  # PERCENT despite the name (28.8 = +28.8%) — see ``return_fraction``
    contribution_pp: float  # PERCENTAGE POINTS — see ``contribution_fraction``

    @property
    def return_fraction(self) -> float:
        return self.return_pct / 100.0

    @property
    def contribution_fraction(self) -> float:
        return self.contribution_pp / 100.0


@dataclass(frozen=True)
class IndexContributionsArtifact:
    schema_version: int
    index: str
    proxy_symbol: str
    # Named `trading_day`, deliberately NOT `as_of` (metron-ops-I346, 2026-09-21) — the
    # fleet's run-timestamp-provenance convention (nousergon-data's
    # test_run_timestamp_fields_are_declared_provenance_where_a_contract_declares_them)
    # treats `as_of` as a member of the run-timestamp class and requires it be marked
    # `x-provenance: true`, which would have been WRONG here: this is the trading
    # SESSION the decomposition describes, a DATA value — reproducing 2026-09-21 must
    # yield 2026-09-21, and a shadow run that decomposed the wrong day has to be FLAGGED,
    # not excluded from parity comparison the way a provenance field would be. Do not
    # rename this back to `as_of` to match other contracts; the mismatch is intentional.
    trading_day: date
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
    spine reader) rather than raising into a request handler.

    ``raw["trading_day"]`` is REQUIRED — a payload carrying only the old `as_of` key (or
    missing the field entirely) is malformed, not a hit with a null date: ``raw["trading_day"]``
    (not ``.get``) raises ``KeyError`` on a missing key, which the ``except`` below turns
    into an honest ``None`` rather than a silently-null session date. A null trading day
    would let the caller reconcile a decomposition against the wrong day's alpha — the
    one failure mode the reconciliation gate can't catch on its own, since it only checks
    that the numbers tie, not that they're tied to the right day."""
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
            trading_day=date.fromisoformat(raw["trading_day"]),
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
    if art is not None and art.trading_day != as_of:
        # Defensive: a source (e.g. a stale `latest.json` fallback) returning a
        # different day's decomposition than asked for is a mismatch, not a hit — never
        # silently substitute one day's story for another's.
        logger.warning("index-contributions artifact for %s dated %s, expected %s", index, art.trading_day, as_of)
        return None
    return art
