"""GICS sector taxonomy + the sector/benchmark source seams.

A holding's sector (portfolio side) and the benchmark's sector weights (SPY side) are
the reference data Brinson-Fachler attribution needs beyond prices. Both are sourced
through injectable callables (tests inject deterministic maps). The DEFAULT is the
**data spine** — Metron reads sectors from `alpha-engine-data`'s S3 artifact and makes
no direct classification fetch (imported lazily, so importing this module needs no
boto3/network).

Fail-soft by symbol, mirroring the price source: a symbol the source can't classify is
absent from the result (its market value lands in the "unclassified" coverage gap,
never silently attributed to a guessed sector).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

# Canonical GICS sector label (yfinance Title-Case, as returned by ``Ticker.info``)
# → its SPDR sector ETF. The 11 GICS sectors SPY is decomposed into for the benchmark.
SECTOR_ETF: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}

# yfinance ``funds_data.sector_weightings`` snake_case keys → canonical sector label.
# (Kept for parity with the producer, which canonicalizes before publishing.)
FUNDS_SECTOR_KEY: dict[str, str] = {
    "technology": "Technology",
    "financial_services": "Financial Services",
    "healthcare": "Healthcare",
    "consumer_cyclical": "Consumer Cyclical",
    "consumer_defensive": "Consumer Defensive",
    "energy": "Energy",
    "industrials": "Industrials",
    "basic_materials": "Basic Materials",
    "utilities": "Utilities",
    "realestate": "Real Estate",
    "communication_services": "Communication Services",
}

# Sector-vocabulary drift map (metron-ops#93). The canonical taxonomy is the yfinance
# Title-Case one — the ``SECTOR_ETF`` keys, which ``Ticker.info['sector']`` returns and
# which the benchmark sector weights are keyed by. A second, non-spine write path leaked
# GICS-proper labels ("Information Technology", "Health Care") and a few common synonyms
# into ``securities.sector``, so the same GICS sector rendered as two rows in the
# Allocation "By sector" breakdown. ``canonical_sector`` folds every known variant onto
# its ``SECTOR_ETF`` key BEFORE the value is persisted/grouped — fixing the split at the
# source seam instead of at each read site. Aliases are matched case-insensitively (see
# ``canonical_sector``), so pure-casing variants need no separate entry here.
SECTOR_ALIASES: dict[str, str] = {
    # GICS-proper → yfinance Title-Case canonical
    "information technology": "Technology",
    "health care": "Healthcare",
    "financials": "Financial Services",
    "financial": "Financial Services",
    "materials": "Basic Materials",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "telecommunication services": "Communication Services",
    "telecommunications": "Communication Services",
    # common synonyms / underscored variants
    "tech": "Technology",
    "information_technology": "Technology",
}

# Lower-cased lookup folding both the canonical labels (for idempotency) and every alias
# onto the canonical ``SECTOR_ETF`` key. Built once at import.
_CANONICAL_BY_LOWER: dict[str, str] = {
    **{label.lower(): label for label in SECTOR_ETF},
    **{alias.lower(): canonical for alias, canonical in SECTOR_ALIASES.items()},
}


def canonical_sector(label: str | None) -> str | None:
    """Fold a raw sector ``label`` onto the canonical yfinance Title-Case taxonomy (the
    ``SECTOR_ETF`` keys). Idempotent on already-canonical values; case- and
    whitespace-insensitive. Empty/None → ``None``. An unrecognized label is returned
    trimmed but otherwise unchanged — never guessed; it just isn't a drift variant we
    know how to fold (e.g. the custom "Broad Market / Index" index-ETF label)."""
    if not label:
        return None
    stripped = label.strip()
    if not stripped:
        return None
    return _CANONICAL_BY_LOWER.get(stripped.lower(), stripped)


# A sector source maps symbols → each symbol's canonical GICS label. Default = data spine.
SectorSource = Callable[[list[str]], dict[str, str]]
# A country source maps symbols → each symbol's country of domicile. Default = data spine.
CountrySource = Callable[[list[str]], dict[str, str]]
# A benchmark source returns the benchmark's GICS sector weights (canonical → fraction).
BenchmarkSource = Callable[[], dict[str, float]]


def fetch_sectors(symbols: Iterable[str], *, source: SectorSource | None = None) -> dict[str, str]:
    """GICS sector per symbol. Deduped, order-insensitive.

    Returns ``{}`` for empty input. Symbols the source can't classify are omitted
    (the caller leaves their ``sector`` NULL → counted against coverage, not guessed).
    Every resolved label is folded through ``canonical_sector`` (metron-ops#93), so the
    one canonical taxonomy reaches the persistence/grouping seam regardless of which
    source path produced it — fixing sector-vocabulary drift at the writer."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    if source is None:
        from portfolio_analytics.sectors.spine_source import spine_sectors
        source = spine_sectors
    return {sym: c for sym, label in source(unique).items() if (c := canonical_sector(label))}


def fetch_countries(symbols: Iterable[str], *, source: CountrySource | None = None) -> dict[str, str]:
    """Country of domicile per symbol. Deduped, order-insensitive.

    Returns ``{}`` for empty input. Symbols the source can't classify are omitted
    (the caller leaves their ``country`` NULL → counted against coverage, not guessed).
    Same source seam as ``fetch_sectors`` — defaults to the data spine; the multi-tenant
    tier swaps in a licensed feed via ``source`` rather than editing callers."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    if source is None:
        from portfolio_analytics.sectors.spine_source import spine_countries
        source = spine_countries
    return source(unique)


def fetch_benchmark_sector_weights(*, source: BenchmarkSource | None = None) -> dict[str, float]:
    """The benchmark's GICS sector weights (canonical label → raw fraction).

    Returns ``{}`` on any failure — the caller then can't build a benchmark and the
    attribution degrades to not-computable WITH a reason, never to a fabricated split.
    Keys are folded through ``canonical_sector`` (metron-ops#93) so the benchmark side
    of the attribution join uses the same taxonomy as the portfolio side; if two drift
    variants collapse onto one canonical label their fractions are summed."""
    if source is None:
        from portfolio_analytics.sectors.spine_source import spine_benchmark_sector_weights
        source = spine_benchmark_sector_weights
    out: dict[str, float] = {}
    for label, weight in source().items():
        canonical = canonical_sector(label)
        if canonical:
            out[canonical] = out.get(canonical, 0.0) + weight
    return out
