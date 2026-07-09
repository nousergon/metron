"""Concentration & diversification diagnostics — deterministic portfolio-structure FACTS.

The engine behind the Intelligence-lane diagnostics card (metron-ops-I167, pre-registration
lane metron-ops-I164). Everything here is a measurement, never advice:

  - **sector weights** vs a benchmark's GICS sector weights (over/underweight deltas);
  - **geography split** (US / International / Unclassified, by country of domicile);
  - **concentration metrics** — HHI, effective number of positions, top-5/top-10 NAV
    share, largest single position;
  - **user-target drift** — a mechanical evaluation of the user's OWN stated targets
    (the ``AdvisorProfile`` KEEP class per metron-ops-I166): target_allocation vs
    actual, max_single_position breaches, avoid_sectors present in holdings. The copy
    pattern is "you set X; actual is Y" — the user authored the rule, this engine only
    reports the measurement. NO LLM anywhere; no prescriptive output.

Pure stdlib + the sector/geo reference data in ``portfolio_analytics.sectors`` —
data-source-agnostic and unit-testable without the API/DB stack. The caller supplies
already-valued positions (SETTLED base-currency market values only — the live/settled
isolation invariant; and holdings only, never watchlist entries — the structural
watchlist isolation invariant) and the raw benchmark weights.

No fabrication: an unclassified sector/country lands in an explicit ``Unclassified``
bucket; a missing benchmark yields ``benchmark_available = False`` with every delta
``None`` — never a guessed or hardcoded weight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from portfolio_analytics.sectors import SECTOR_ETF, canonical_sector, geo_bucket

# Bucket label for market value whose sector/country the reference source couldn't
# resolve — a coverage gap surfaced honestly, never folded into a guessed bucket.
UNCLASSIFIED = "Unclassified"

# The ``target_allocation`` keys the engine can measure mechanically from holdings
# metadata today (the US-vs-international domicile split). Any other key the user
# authored is reported with ``actual = None`` ("not measurable") rather than guessed.
MEASURABLE_ALLOCATION_KEYS: frozenset[str] = frozenset({"us_equity", "international"})


@dataclass(frozen=True)
class DiagnosticsPosition:
    """One valued position, as the engine consumes it.

    ``market_value`` is the SETTLED base-currency value and must be positive — the
    caller filters unpriced / non-positive / cash rows out (mirroring the attribution
    engine's contract) and this engine fails loud on a violation rather than producing
    weights that don't sum to 1.
    """

    ticker: str
    market_value: float
    sector: str | None = None
    country: str | None = None


@dataclass(frozen=True)
class StatedTargets:
    """The user-authored target fields of the investor profile (the mechanical KEEP
    class per metron-ops-I166) — the ONLY profile fields this engine ever sees.
    Suitability fields (risk_tolerance / time_horizon / strategy) are structurally
    excluded: they are not part of this type."""

    # Fractions of total portfolio, e.g. {"us_equity": 0.60, "international": 0.15}.
    target_allocation: Mapping[str, float] = field(default_factory=dict)
    max_single_position: float | None = None  # fraction, e.g. 0.10 = 10%
    avoid_sectors: tuple[str, ...] = ()

    def has_any(self) -> bool:
        """Whether the user authored at least one evaluable target — the drift section
        renders only when this is true."""
        return bool(self.target_allocation) or self.max_single_position is not None or bool(self.avoid_sectors)

    @classmethod
    def from_profile_block(cls, block: Mapping | None) -> StatedTargets | None:
        """Extract the target fields from an ``investor_profile``-shaped config block
        (the persisted ``AdvisorProfile.block`` schema, mirrored publicly in
        ``web/lib/api.ts``). Returns ``None`` when the block carries no targets, so a
        profile that only filled suitability fields reads as "no targets authored"."""
        if not block:
            return None
        sector_prefs = block.get("sector_preferences") or {}
        targets = cls(
            target_allocation={
                str(k): float(v) for k, v in (block.get("target_allocation") or {}).items()
            },
            max_single_position=(
                float(block["max_single_position"])
                if block.get("max_single_position") is not None
                else None
            ),
            avoid_sectors=tuple(str(s) for s in (sector_prefs.get("avoid") or []) if s),
        )
        return targets if targets.has_any() else None


@dataclass(frozen=True)
class SectorRow:
    """One sector's portfolio weight next to the benchmark's. ``benchmark_weight`` /
    ``delta`` are ``None`` when the benchmark is unavailable, or for buckets the
    benchmark has no concept of (``Unclassified``, non-GICS custom labels)."""

    sector: str
    weight: float  # share of included (priced, non-cash) market value
    market_value: float
    benchmark_weight: float | None = None
    delta: float | None = None  # weight − benchmark_weight (overweight > 0)


@dataclass(frozen=True)
class GeoRow:
    """One geography bucket's share of included market value."""

    bucket: str  # "US" | "International" | "Unclassified"
    weight: float
    market_value: float


@dataclass(frozen=True)
class ConcentrationMetrics:
    """Position-level concentration measurements over the included market value."""

    n_positions: int
    # Herfindahl-Hirschman index: Σ wᵢ² over per-position weight fractions. 1/N for an
    # equal-weight portfolio of N positions; 1.0 for a single position.
    hhi: float
    # 1 / HHI — the number of equal-weight positions with the same concentration.
    effective_n: float
    top5_share: float
    top10_share: float
    max_position_ticker: str
    max_position_weight: float


@dataclass(frozen=True)
class TargetDriftRow:
    """One mechanical evaluation of a user-authored target: "you set ``target``;
    actual is ``actual``". ``breach`` is the rule's boolean outcome where the target IS
    a rule (max position / avoid sector) — ``None`` for pure drift rows (allocation)
    and for targets the engine cannot measure (``actual is None``)."""

    kind: str  # "allocation" | "max_position" | "avoid_sector"
    label: str
    target: float | None
    actual: float | None
    breach: bool | None = None
    detail: str | None = None  # e.g. the tickers held in an avoided sector


@dataclass(frozen=True)
class DiagnosticsResult:
    computable: bool
    reason: str | None = None
    total_market_value: float = 0.0
    benchmark_available: bool = False
    sectors: tuple[SectorRow, ...] = ()
    geography: tuple[GeoRow, ...] = ()
    concentration: ConcentrationMetrics | None = None
    # ``None`` = the user has authored no targets (the drift section doesn't render);
    # ``()`` cannot occur — authored targets always produce at least one row.
    target_drift: tuple[TargetDriftRow, ...] | None = None


def normalize_benchmark_weights(raw: Mapping[str, float] | None) -> dict[str, float]:
    """Restrict raw benchmark weights to the known GICS sectors (folding drift variants
    through ``canonical_sector``) and renormalize to sum to 1. ``{}`` in → ``{}`` out —
    the caller then reports the benchmark as unavailable, never fabricated."""
    if not raw:
        return {}
    kept: dict[str, float] = {}
    for label, weight in raw.items():
        canonical = canonical_sector(label)
        if canonical in SECTOR_ETF and weight > 0:
            kept[canonical] = kept.get(canonical, 0.0) + weight
    total = sum(kept.values())
    return {s: w / total for s, w in kept.items()} if total > 0 else {}


def _sector_rows(
    positions: Sequence[DiagnosticsPosition], total: float, bench: dict[str, float]
) -> tuple[SectorRow, ...]:
    """Union of held sectors and benchmark sectors, so an unheld benchmark sector shows
    as an explicit underweight (weight 0) instead of silently missing."""
    by_sector: dict[str, float] = {}
    for p in positions:
        label = canonical_sector(p.sector) or UNCLASSIFIED
        by_sector[label] = by_sector.get(label, 0.0) + p.market_value
    rows = []
    for label in set(by_sector) | set(bench):
        mv = by_sector.get(label, 0.0)
        weight = mv / total
        bench_w = bench.get(label) if (bench and label in SECTOR_ETF) else None
        rows.append(
            SectorRow(
                sector=label,
                weight=weight,
                market_value=mv,
                benchmark_weight=bench_w,
                delta=(weight - bench_w) if bench_w is not None else None,
            )
        )
    # Heaviest portfolio weight first; unheld benchmark sectors by their bench weight;
    # the Unclassified coverage bucket always last.
    rows.sort(key=lambda r: (r.sector == UNCLASSIFIED, -r.weight, -(r.benchmark_weight or 0.0), r.sector))
    return tuple(rows)


def _geo_rows(positions: Sequence[DiagnosticsPosition], total: float) -> tuple[GeoRow, ...]:
    by_bucket: dict[str, float] = {"US": 0.0, "International": 0.0, UNCLASSIFIED: 0.0}
    for p in positions:
        by_bucket[geo_bucket(p.country)] += p.market_value
    return tuple(
        GeoRow(bucket=b, weight=mv / total, market_value=mv)
        for b, mv in by_bucket.items()
        if mv > 0 or b != UNCLASSIFIED  # US/Intl always shown; Unclassified only when real
    )


def _concentration(positions: Sequence[DiagnosticsPosition], total: float) -> ConcentrationMetrics:
    weights = sorted((p.market_value / total, p.ticker) for p in positions)
    weights.reverse()  # heaviest first
    hhi = sum(w * w for w, _ in weights)
    return ConcentrationMetrics(
        n_positions=len(weights),
        hhi=hhi,
        effective_n=1.0 / hhi,  # hhi > 0 — positions are non-empty with positive MV
        top5_share=sum(w for w, _ in weights[:5]),
        top10_share=sum(w for w, _ in weights[:10]),
        max_position_ticker=weights[0][1],
        max_position_weight=weights[0][0],
    )


def _allocation_drift(
    targets: StatedTargets, geo: Sequence[GeoRow]
) -> list[TargetDriftRow]:
    """Evaluate ``target_allocation`` mechanically. The us_equity/international pair is
    measured against the US-vs-International split of CLASSIFIED market value, with the
    targets normalized to sum to 1 within the pair — mirroring the advisor profile's
    ``equity_geo_targets`` semantics so the two surfaces never disagree. A key the
    engine can't measure from holdings metadata is reported with ``actual = None``,
    never guessed."""
    rows: list[TargetDriftRow] = []
    alloc = targets.target_allocation
    us_t, intl_t = alloc.get("us_equity"), alloc.get("international")
    pair_measurable = us_t is not None and intl_t is not None and (us_t + intl_t) > 0
    geo_mv = {r.bucket: r.market_value for r in geo}
    classified = geo_mv.get("US", 0.0) + geo_mv.get("International", 0.0)
    for key in sorted(alloc):
        value = alloc[key]
        if key in MEASURABLE_ALLOCATION_KEYS and pair_measurable:
            bucket = "US" if key == "us_equity" else "International"
            target_norm = value / (us_t + intl_t)
            actual = (geo_mv.get(bucket, 0.0) / classified) if classified > 0 else None
            rows.append(
                TargetDriftRow(
                    kind="allocation",
                    label=f"{bucket} share of classified holdings",
                    target=target_norm,
                    actual=actual,
                    detail=None if actual is not None else "no holdings have a resolved country",
                )
            )
        else:
            # Either an unknown vocabulary key, or only one of the us_equity/international
            # pair was authored (not normalizable the way the profile defines the split).
            rows.append(
                TargetDriftRow(
                    kind="allocation",
                    label=key,
                    target=value,
                    actual=None,
                    detail=(
                        "requires both us_equity and international targets"
                        if key in MEASURABLE_ALLOCATION_KEYS
                        else "not measurable from holdings metadata"
                    ),
                )
            )
    return rows


def _max_position_drift(
    targets: StatedTargets, positions: Sequence[DiagnosticsPosition], total: float
) -> list[TargetDriftRow]:
    """One row per position EXCEEDING the stated max (a position exactly AT the max is
    within the user's rule — strict inequality); when nothing breaches, one row showing
    the largest position against the limit so the rule's headroom is still visible."""
    if targets.max_single_position is None:
        return []
    limit = targets.max_single_position
    weighted = sorted(((p.market_value / total, p.ticker) for p in positions), reverse=True)
    breaches = [
        TargetDriftRow(kind="max_position", label=ticker, target=limit, actual=w, breach=True)
        for w, ticker in weighted
        if w > limit
    ]
    if breaches:
        return breaches
    top_w, top_ticker = weighted[0]
    return [TargetDriftRow(kind="max_position", label=top_ticker, target=limit, actual=top_w, breach=False)]


def _avoid_sector_drift(
    targets: StatedTargets, positions: Sequence[DiagnosticsPosition], total: float
) -> list[TargetDriftRow]:
    """One row per avoided sector: held weight + the offending tickers when present,
    weight 0 when clean. User labels fold through ``canonical_sector`` so "health care"
    matches holdings classified "Healthcare"."""
    by_sector: dict[str, list[tuple[float, str]]] = {}
    for p in positions:
        label = canonical_sector(p.sector)
        if label:
            by_sector.setdefault(label, []).append((p.market_value, p.ticker))
    rows: list[TargetDriftRow] = []
    for raw in targets.avoid_sectors:
        sector = canonical_sector(raw)
        if not sector:
            continue
        held = by_sector.get(sector, [])
        mv = sum(v for v, _ in held)
        rows.append(
            TargetDriftRow(
                kind="avoid_sector",
                label=sector,
                target=0.0,
                actual=mv / total,
                breach=mv > 0,
                detail=", ".join(t for _, t in sorted(held, reverse=True)) or None,
            )
        )
    return rows


def evaluate_target_drift(
    targets: StatedTargets,
    positions: Sequence[DiagnosticsPosition],
    total: float,
    geo: Sequence[GeoRow],
) -> tuple[TargetDriftRow, ...]:
    """Mechanically evaluate every authored target against the included positions."""
    return (
        *_allocation_drift(targets, geo),
        *_max_position_drift(targets, positions, total),
        *_avoid_sector_drift(targets, positions, total),
    )


def compute_diagnostics(
    positions: Sequence[DiagnosticsPosition],
    *,
    benchmark_weights: Mapping[str, float] | None = None,
    targets: StatedTargets | None = None,
) -> DiagnosticsResult:
    """Compute the full diagnostics card payload from valued positions.

    ``positions`` must already be the INCLUDED set: priced, positive-market-value,
    non-cash holdings (never watchlist entries) in one base currency. Empty input is a
    valid state (not-computable WITH a reason); a non-positive market value is a caller
    contract violation and raises."""
    for p in positions:
        if p.market_value <= 0:
            raise ValueError(
                f"DiagnosticsPosition {p.ticker!r} has non-positive market_value "
                f"{p.market_value!r} — the caller must filter unpriced/non-positive rows"
            )
    if not positions:
        return DiagnosticsResult(
            computable=False, reason="No priced non-cash holdings — refresh prices first."
        )
    total = sum(p.market_value for p in positions)
    bench = normalize_benchmark_weights(benchmark_weights)
    geo = _geo_rows(positions, total)
    return DiagnosticsResult(
        computable=True,
        total_market_value=total,
        benchmark_available=bool(bench),
        sectors=_sector_rows(positions, total, bench),
        geography=geo,
        concentration=_concentration(positions, total),
        target_drift=(
            evaluate_target_drift(targets, positions, total, geo)
            if targets is not None and targets.has_any()
            else None
        ),
    )
