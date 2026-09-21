"""Name-level explanation of the holdings-vs-benchmark return gap (metron-ops-I346).

``api/services/performance.py:period_tiles`` already shows "you +1.8%, Nasdaq-100
+2.0%" (``BenchmarkReturn.alpha``). This answers *why*: which names in the index moved
it that the portfolio doesn't hold (or holds at a different weight), via
``nousergon_lib.quant.attribution.security_contributions`` — the security-level sibling
of the sector-level Brinson-Fachler ``attribution.py`` already uses.

**Boundary this respects (metron-ops-I346):** tenant holdings never leave Metron's
database. The collector (``alpha-engine-config-I11297``) owns every market-side fact —
index membership, weights, per-name returns; this module reads that artifact through
``portfolio_analytics.index_contributions`` (a pure S3 read, injectable — see that
module for the percentage-point/fraction unit boundary, the likeliest defect here) and
computes only the portfolio-side prior-close weights + the difference.

**Correctness gate (deliverable 3, the single most important test in the issue):** the
decomposition must sum to the alpha ``performance.py`` already displays, within
``tolerance``. ``nousergon_lib.quant.attribution.reconcile`` computes that residual as a
first-class value; when it's outside tolerance this service REFUSES (``computable=False``
with the residual named in ``reason``) rather than returning a plausible-looking driver
list — a driver list that doesn't tie to the number on screen is worse than none, because
it's equally convincing.

**Stage A only** (ruling R5, 2026-09-15): deterministic decomposition + a factual
``earnings`` event tag (from ``market_data/earnings/latest.json``). No generated prose —
that's Stage B, out of scope here.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

from nousergon_lib.quant.attribution import SecurityContribution, reconcile, security_contributions, top_drivers
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.calendar import fetch_earnings_dates
from portfolio_analytics.index_contributions import (
    IndexContributionsArtifact,
    IndexContributionsSource,
    fetch_index_contributions,
)

logger = logging.getLogger(__name__)

# The two indexes this ships for (metron-ops-I346) — the proxy each maps to is read from
# the artifact itself (``IndexContributionsArtifact.proxy_symbol``); this is display-only.
INDEX_LABELS: dict[str, str] = {"SPX": "S&P 500", "NDX": "Nasdaq 100"}

# Return-fraction slack for the reconciliation gate: floating-point + same-day rounding
# budget across two independently-sourced numbers (the settled NAV-snapshot portfolio
# return and the collector's index-constituent decomposition) — not a business-materiality
# judgment (that's the caller's, per ``reconcile``'s own contract). 5bps.
DEFAULT_TOLERANCE = 0.0005
DEFAULT_TOP_N = 15


@dataclass
class GapDriver:
    """One name's active-return contribution (metron-ops-I346). All return-fraction units."""

    symbol: str
    classification: str  # held / not_held / overweight / underweight (from nousergon_lib)
    port_weight: float
    bench_weight: float
    active_weight: float
    ret: float
    port_contribution: float
    bench_contribution: float
    active_contribution: float
    earnings: bool = False  # factual tag: next earnings date lands this session or the prior evening


@dataclass
class BenchmarkGapSummary:
    computable: bool
    reason: str | None = None
    required_tier: str | None = None
    index: str | None = None
    index_label: str | None = None
    proxy_symbol: str | None = None
    as_of: date | None = None
    weight_method: str | None = None
    coverage_weight_with_return: float | None = None
    coverage_members: int | None = None
    coverage_members_missing_return: int | None = None
    portfolio_return: float | None = None
    benchmark_return: float | None = None
    active_return: float | None = None  # the alpha performance.py already displays
    residual: float | None = None
    within_tolerance: bool = False
    tolerance: float = DEFAULT_TOLERANCE
    missing_returns: list[str] = field(default_factory=list)
    drivers: list[GapDriver] = field(default_factory=list)


# ── Module-level artifact cache ───────────────────────────────────────────────────────
# Several portfolios/requests in the same process ask for the SAME (index, date)
# decomposition; without this every one of them re-reads S3. Mirrors
# ``api.services.market_snapshot``'s single-flight TTL cache pattern. Tests must clear
# this (``tests/conftest.py``'s autouse ``_clear_compute_cache`` calls ``clear_cache()``
# below) or one test's injected fixture would bleed into the next.
_ARTIFACT_TTL_S = 300.0
_artifact_lock = threading.Lock()
_artifact_cache: dict[tuple[str, str], tuple[float, IndexContributionsArtifact | None]] = {}


def clear_cache() -> None:
    """Drop all cached artifacts — for tests, so one test's injected fixture never
    bleeds into another's."""
    with _artifact_lock:
        _artifact_cache.clear()


def _cached_artifact(
    index: str, as_of: date, *, source: IndexContributionsSource | None = None
) -> IndexContributionsArtifact | None:
    key = (index, as_of.isoformat())
    now = time.monotonic()
    with _artifact_lock:
        hit = _artifact_cache.get(key)
        if hit is not None and now - hit[0] < _ARTIFACT_TTL_S:
            return hit[1]
    art = fetch_index_contributions(index, as_of, source=source)
    with _artifact_lock:
        _artifact_cache[key] = (now, art)
    return art


def _earnings_tags(symbols: list[str], *, today: date, earnings_source=None) -> set[str]:
    """Symbols whose next-scheduled earnings date is dated for ``today``'s session or the
    prior evening (an after-close report the market is reacting to this session). Only
    a DATE is published (``market_data/earnings/latest.json``), not a before/after-close
    flag, so "prior evening" is approximated as the prior calendar day — a factual tag,
    never generated prose (Stage B, ruling R5, is out of scope here)."""
    if not symbols:
        return set()
    dates = fetch_earnings_dates(symbols, source=earnings_source)
    prior = today - timedelta(days=1)
    return {sym for sym, d in dates.items() if d in (today, prior)}


def _portfolio_prior_close_weights_and_returns(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, float], dict[str, float]] | None:
    """Prior-close portfolio weights (``leg.value / start.nav``) + each held name's
    total return over the window (price x FX, base currency) — from the two settled
    ``NavSnapshot`` rows bounding the window, mirroring ``api/services/glance.py``'s
    ``_movers`` settled path exactly (same two-snapshot-legs mechanic, same
    quantity-change-is-a-flow-not-a-move exclusion; the residual bucket the reconciliation
    gate computes absorbs same-day trades, cash and any other untracked drift).

    None when the composition isn't available for either boundary snapshot — the caller
    renders not-computable, never a fabricated weight."""
    snaps = {
        s.snap_date: s
        for s in session.scalars(
            select(models.NavSnapshot).where(
                models.NavSnapshot.tenant_id == tenant_id,
                models.NavSnapshot.portfolio_id == portfolio_id,
                models.NavSnapshot.snap_date.in_([start_date, end_date]),
            )
        ).all()
    }
    start, end = snaps.get(start_date), snaps.get(end_date)
    if start is None or end is None or not start.composition or not end.composition:
        return None
    nav0 = float(start.nav) if start.nav else 0.0
    if nav0 <= 0:
        return None
    prev_legs = {leg["ticker"]: leg for leg in start.composition.get("legs", [])}
    weights: dict[str, float] = {}
    returns: dict[str, float] = {}
    for leg in end.composition.get("legs", []):
        ticker = leg.get("ticker")
        prev = prev_legs.get(ticker)
        if prev is None or ticker is None:
            continue  # not held at the start of the window — no prior-close weight
        v0 = prev.get("value")
        if v0 is None:
            continue
        weights[ticker] = float(v0) / nav0
        p0, p1 = prev.get("price"), leg.get("price")
        fx0, fx1 = prev.get("fx_rate"), leg.get("fx_rate")
        if p0 not in (None, 0) and p1 is not None and fx0 not in (None, 0) and fx1 is not None:
            returns[ticker] = (float(p1) * float(fx1)) / (float(p0) * float(fx0)) - 1.0
    return weights, returns


def compute_benchmark_gap(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    index: str,
    tile,  # api.services.performance.PeriodTile — the caller's already-computed TODAY tile
    account_ids: Collection[uuid.UUID] | None = None,
    top_n: int = DEFAULT_TOP_N,
    tolerance: float = DEFAULT_TOLERANCE,
    source: IndexContributionsSource | None = None,
    earnings_source=None,
) -> BenchmarkGapSummary:
    """Name-level active-return drivers explaining ``tile``'s benchmark alpha for
    ``index`` ("SPX" or "NDX"). ``tile`` is the SAME "today" ``PeriodTile`` the caller
    already computed via ``performance.period_tiles`` (with ``today_bench`` wired the
    same way the Overview does) — the alpha this reconciles against is exactly the one
    already on screen, not a re-derived one.

    A missing artifact / uncached benchmark / unformed window all degrade to
    ``computable=False`` WITH a reason (never "no drivers" — absence of data is not a
    finding of no drivers, per the issue's gotcha). A residual outside ``tolerance``
    ALSO degrades to ``computable=False`` — the correctness gate: presenting drivers
    that don't reconcile to the displayed alpha would be showing a story, not the
    reconciled truth."""
    if index not in INDEX_LABELS:
        raise ValueError(f"unknown index {index!r}; known: {sorted(INDEX_LABELS)}")
    label = INDEX_LABELS[index]

    if tile is None or tile.start_date is None or tile.end_date is None or tile.twr is None:
        return BenchmarkGapSummary(
            computable=False,
            reason="Not enough NAV history to compute today's window yet.",
            index=index, index_label=label,
        )

    artifact = _cached_artifact(index, tile.end_date, source=source)
    if artifact is None:
        return BenchmarkGapSummary(
            computable=False,
            reason=f"No {label} constituent decomposition published for {tile.end_date.isoformat()} yet.",
            index=index, index_label=label,
        )

    bench = next((b for b in tile.benchmarks if b.symbol == artifact.proxy_symbol), None)
    if bench is None or bench.ret is None or bench.alpha is None:
        return BenchmarkGapSummary(
            computable=False,
            reason=f"{artifact.proxy_symbol} benchmark return unavailable for today — refresh prices first.",
            index=index, index_label=label, proxy_symbol=artifact.proxy_symbol, as_of=artifact.as_of,
        )

    port = _portfolio_prior_close_weights_and_returns(
        session, tenant_id, portfolio_id, start_date=tile.start_date, end_date=tile.end_date
    )
    if port is None:
        return BenchmarkGapSummary(
            computable=False,
            reason="Portfolio composition unavailable for today's window.",
            index=index, index_label=label, proxy_symbol=artifact.proxy_symbol, as_of=artifact.as_of,
        )
    port_weights, port_returns = port
    if account_ids is not None:
        # The composition legs aren't account-scoped; an ?account_id= selection has no
        # sub-portfolio composition to key off yet, so name-level weights degrade
        # honestly rather than silently mixing the whole-portfolio legs with a
        # scoped alpha.
        return BenchmarkGapSummary(
            computable=False,
            reason="Name-level benchmark-gap drivers are whole-portfolio only (account selection not yet supported).",
            index=index, index_label=label, proxy_symbol=artifact.proxy_symbol, as_of=artifact.as_of,
        )

    bench_weights = {c.symbol: c.weight_prior_close for c in artifact.constituents}
    # .return_fraction, never the raw .return_pct — return_pct is PERCENT (28.8 = +28.8%),
    # not a fraction (metron-ops-I346 units correction, 2026-09-21). nousergon_lib works
    # entirely in fractions.
    returns = {c.symbol: c.return_fraction for c in artifact.constituents}
    for sym, r in port_returns.items():
        returns.setdefault(sym, r)

    result = security_contributions(port_weights, bench_weights, returns)
    recon = reconcile(result.contributions, portfolio_return=tile.twr, benchmark_return=bench.ret, tolerance=tolerance)

    base = BenchmarkGapSummary(
        computable=False,
        index=index, index_label=label, proxy_symbol=artifact.proxy_symbol, as_of=artifact.as_of,
        weight_method=artifact.weight_method,
        coverage_weight_with_return=artifact.coverage_weight_with_return,
        coverage_members=artifact.coverage_members,
        coverage_members_missing_return=artifact.coverage_members_missing_return,
        portfolio_return=tile.twr, benchmark_return=bench.ret, active_return=bench.alpha,
        residual=recon.residual, within_tolerance=recon.within_tolerance, tolerance=tolerance,
        missing_returns=result.missing,
    )
    if not recon.within_tolerance:
        base.reason = (
            f"Decomposition does not reconcile to the displayed alpha "
            f"(residual {recon.residual:+.4%} vs tolerance {tolerance:.2%}) — refusing to show drivers."
        )
        return base

    top: list[SecurityContribution] = top_drivers(result.contributions, top_n)
    tagged = _earnings_tags([c.symbol for c in top], today=tile.end_date, earnings_source=earnings_source)
    base.computable = True
    base.drivers = [
        GapDriver(
            symbol=c.symbol,
            classification=c.classification,
            port_weight=c.port_weight,
            bench_weight=c.bench_weight,
            active_weight=c.port_weight - c.bench_weight,
            ret=c.ret,
            port_contribution=c.port_contribution,
            bench_contribution=c.bench_contribution,
            active_contribution=c.active_contribution,
            earnings=c.symbol in tagged,
        )
        for c in top
    ]
    return base
