"""Concentration & diversification diagnostics for a portfolio (metron-ops-I167).

The API-side assembly around the pure engine in
``portfolio_analytics.domain.diagnostics``: values the holdings on the SETTLED
context (official EOD closes — never the intraday overlay; the live/settled isolation
invariant), resolves each holding's GICS sector + country of domicile exactly as the
Holdings surfaces do (tenant overrides win over the spine value), fetches the
benchmark's sector weights from the data spine, loads the user's stated targets
through the plugin capability seam, and hands everything to the engine.

Everything on the card is a FACT (Intelligence lane, metron-ops-I164): measurements,
benchmark-relative weights, and mechanical evaluation of the user's OWN authored
targets. No LLM anywhere; nothing prescriptive.

Watchlist entries are structurally excluded — the holdings source reads positions/
ledger state only, and watchlist tickers never have a position row (the structural
isolation invariant).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from api.plugins import investor_targets
from api.services import analytics
from api.services import classifications as classifications_service
from api.services import countries as countries_service
from api.services import sectors as sectors_service
from portfolio_analytics.domain.diagnostics import (
    ConcentrationMetrics,
    DiagnosticsPosition,
    GeoRow,
    SectorRow,
    StatedTargets,
    TargetDriftRow,
    compute_diagnostics,
)
from portfolio_analytics.sectors import BenchmarkSource, fetch_benchmark_sector_weights

BENCHMARK = "SPY"


@dataclass
class DiagnosticsSummary:
    computable: bool
    reason: str | None = None
    # Set only when not computable because the product tier excludes the feature —
    # the cheapest tier that would unlock it (drives the entitlement upsell).
    required_tier: str | None = None
    # The settled close date the valuation is computed from (the freshest
    # last_price_date across included holdings) — the card's as-of badge. Never the
    # request date: an unrefreshed cache would overstate freshness.
    as_of: date | None = None
    base_currency: str = "USD"
    total_market_value: float = 0.0
    benchmark: str = BENCHMARK
    benchmark_available: bool = False
    # Why the benchmark columns are empty: "tier"/"feed" (entitlement — upsell via
    # benchmark_required_tier) or "unavailable" (entitled but the spine artifact is
    # missing/empty). None when available.
    benchmark_reason: str | None = None
    benchmark_required_tier: str | None = None
    concentration: ConcentrationMetrics | None = None
    sectors: list[SectorRow] = field(default_factory=list)
    geography: list[GeoRow] = field(default_factory=list)
    # None = the user has authored no targets (the drift section doesn't render).
    target_drift: list[TargetDriftRow] | None = None


def compute_portfolio_diagnostics(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    base_currency: str = "USD",
    account_ids: Collection[uuid.UUID] | None = None,
    include_benchmark: bool = True,
    benchmark_source: BenchmarkSource | None = None,
    targets_loader=None,
) -> DiagnosticsSummary:
    """The diagnostics payload over the SETTLED valuation of the (account-scoped)
    holdings. ``include_benchmark=False`` skips the spine read entirely (the endpoint
    passes the entitlement verdict — an unentitled deployment never touches the
    benchmark artifact); ``benchmark_source`` / ``targets_loader`` are the injectable
    test seams, mirroring the attribution service."""
    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    # Included set: priced, positive, non-cash. Cash has no sector/geography and would
    # only dilute concentration; unpriced/foreign-no-FX rows can't be weighted (their
    # absence is already surfaced on the Holdings page, never silently guessed here).
    included = [
        h
        for h in held
        if h.security_type != "cash" and h.market_value is not None and h.market_value > 0
    ]

    tickers = [h.ticker for h in included]
    # Resolve sector/country exactly as get_holdings does (ensure_* fills only the
    # still-NULL gaps from the spine, fail-soft; tenant overrides win) so this card
    # groups identically to the Holdings/Allocation surfaces.
    sectors_service.ensure_sectors(session, tickers)
    countries_service.ensure_countries(session, tickers)
    sector_of = sectors_service.sectors_by_symbol(session, tickers)
    country_of = countries_service.countries_by_symbol(session, tickers)
    overrides = classifications_service.overrides_by_symbol(session, tenant_id, tickers)

    positions = []
    for h in included:
        ov = overrides.get(h.ticker)
        positions.append(
            DiagnosticsPosition(
                ticker=h.ticker,
                market_value=h.market_value,
                sector=(ov.sector if ov and ov.sector else None) or sector_of.get(h.ticker),
                country=(ov.country if ov and ov.country else None) or country_of.get(h.ticker),
            )
        )

    raw_bench = fetch_benchmark_sector_weights(source=benchmark_source) if include_benchmark else {}

    # Resolved at call time (not as a default arg) so tests can monkeypatch the
    # module-level ``investor_targets`` name.
    loader = targets_loader if targets_loader is not None else investor_targets
    block = loader(session, tenant_id)
    targets = StatedTargets.from_profile_block(block)

    result = compute_diagnostics(positions, benchmark_weights=raw_bench, targets=targets)

    as_of = max((h.last_price_date for h in included if h.last_price_date), default=None)
    summary = DiagnosticsSummary(
        computable=result.computable,
        reason=result.reason,
        as_of=as_of,
        base_currency=base_currency,
        total_market_value=result.total_market_value,
        benchmark_available=result.benchmark_available,
        benchmark_reason=None if result.benchmark_available else "unavailable",
        concentration=result.concentration,
        sectors=list(result.sectors),
        geography=list(result.geography),
        target_drift=list(result.target_drift) if result.target_drift is not None else None,
    )
    return summary
