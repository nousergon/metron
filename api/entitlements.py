"""Product-tier entitlements — the source of truth for what each Metron tier offers.

Drives the **tier simulator** (owner-only preview of Beta / Pro / Research+ / Base
in the personal build) and, later, real per-tenant subscription gating in the
multi-tenant product. Two orthogonal axes decide whether a feature is shown:

  1. **Tier** — does the active product tier INCLUDE the feature (packaging)?
  2. **Data provisioning** — are the DATA SOURCES the feature needs to COMPUTE
     it available? The no-feed beta provisions only the free sources
     (broker-supplied + self-captured + public-domain); the licensed market-data
     feed provisions the rest (a Pro cost). The "feed toggle" flips axis 2.

A feature is AVAILABLE only when included-in-tier AND computable. Excluded
features carry a ``reason`` (``"tier"`` → upsell; ``"feed"`` / ``"benchmark"`` /
``"etf_vendor"`` → needs the licensed data) so the UI can render an honest
placeholder instead of hiding the surface.

See metron-ops#37 (simulator) and the data-source-vetting doc (which sources are
free vs licensed, and why ETF look-through / benchmark are Pro).
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Data sources a feature can depend on ─────────────────────────────────────
# FREE — always provisioned (broker-sourced + self-captured + public-domain):
FREE_SOURCES: frozenset[str] = frozenset({
    "broker",          # the user's own brokerage holdings/prices (SnapTrade / IBKR Flex)
    "ledger",          # imported / manual transactions
    "snapshots",       # self-captured forward NAV snapshots
    "fred_pubdomain",  # FRED US-gov public-domain macro series
    "edgar",           # SEC EDGAR fundamentals / filings
})
# LICENSED — provisioned only when the market-data feed is on (a Pro cost):
FEED_SOURCES: frozenset[str] = frozenset({
    "feed",        # licensed EOD price history (factor / scenario / backfill)
    "benchmark",   # benchmark index series (alpha vs SPY)
    "etf_vendor",  # licensed ETF-holdings vendor (look-through)
})
ALL_SOURCES: frozenset[str] = FREE_SOURCES | FEED_SOURCES


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    requires: tuple[str, ...]  # data sources required to COMPUTE this feature


# ── The product feature catalog ──────────────────────────────────────────────
FEATURES: tuple[Feature, ...] = (
    Feature("overview", "Portfolio", ("broker",)),
    Feature("income", "Income", ("ledger",)),
    Feature("transactions", "Transactions & realized", ("ledger",)),
    Feature("tax", "Tax (cost-basis + realized/unrealized)", ("ledger", "broker")),
    Feature("concentration", "Concentration & diversification", ("broker",)),
    Feature("performance", "Performance (XIRR + forward TWR)", ("broker", "snapshots")),
    Feature("macro", "Macro", ("fred_pubdomain",)),
    Feature("fundamentals", "Fundamentals", ("edgar",)),
    Feature("auto_sync", "Auto-sync (SnapTrade)", ("broker",)),
    Feature("benchmark", "Benchmark-relative alpha (vs SPY)", ("benchmark",)),
    Feature("risk", "Risk (factor / TE / look-through)", ("feed",)),
    Feature("attribution", "Attribution (Brinson)", ("feed",)),
    Feature("scenarios", "Scenarios / stress", ("feed",)),
    Feature("etf_lookthrough", "ETF look-through", ("etf_vendor",)),
    Feature("agentic_research", "Agentic quant research", ("feed",)),
    Feature("ai_advisor", "AI Advisor", ()),
    Feature("alpha_engine", "Alpha Engine signals", ()),
)
FEATURE_BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}


# ── Tiers (ordered cheapest → most complete; required_tier reads this order) ──
@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    features: frozenset[str]


_BETA = frozenset({
    "overview", "income", "transactions", "tax",
    "concentration", "performance", "macro", "fundamentals",
})
_PRO = _BETA | {"auto_sync", "benchmark", "risk", "attribution", "scenarios", "etf_lookthrough"}
_AGENTIC = _PRO | {"agentic_research"}
_PERSONAL = _AGENTIC | {"ai_advisor", "alpha_engine"}

TIERS: tuple[Tier, ...] = (
    Tier("beta", "Beta (free)", _BETA),
    Tier("pro", "Pro", _PRO),
    Tier("agentic", "Research / Pro+", _AGENTIC),
    Tier("personal", "Base (personal)", _PERSONAL),
)
TIER_BY_KEY: dict[str, Tier] = {t.key: t for t in TIERS}
TIER_ORDER: list[str] = [t.key for t in TIERS]


def provisioned_sources(feed_enabled: bool) -> frozenset[str]:
    """The data sources currently available — free always, licensed iff feed on."""
    return FREE_SOURCES | (FEED_SOURCES if feed_enabled else frozenset())


def required_tier(feature_key: str) -> str | None:
    """The cheapest tier that includes the feature (for the upsell), or None."""
    for t in TIERS:
        if feature_key in t.features:
            return t.key
    return None


def resolve(tier: str, *, feed_enabled: bool) -> dict:
    """Per-feature availability for ``tier`` under the feed toggle.

    ``available = in_tier AND computable``. ``reason`` is ``None`` when available,
    ``"tier"`` when the tier doesn't include it (upsell to ``required_tier``), else
    the first missing data source (``"feed"`` / ``"benchmark"`` / ``"etf_vendor"``).
    """
    if tier not in TIER_BY_KEY:
        raise ValueError(f"unknown tier {tier!r}; known: {TIER_ORDER}")
    prov = provisioned_sources(feed_enabled)
    active = TIER_BY_KEY[tier].features
    features = []
    for f in FEATURES:
        in_tier = f.key in active
        missing = tuple(r for r in f.requires if r not in prov)
        computable = not missing
        available = in_tier and computable
        reason = None if available else ("tier" if not in_tier else missing[0])
        features.append({
            "key": f.key,
            "label": f.label,
            "requires": list(f.requires),
            "available": available,
            "in_tier": in_tier,
            "computable": computable,
            "reason": reason,
            "required_tier": required_tier(f.key),
        })
    return {
        "tier": tier,
        "feed_enabled": feed_enabled,
        "provisioned_sources": sorted(prov),
        "features": features,
        "tiers": [{"key": t.key, "label": t.label} for t in TIERS],
    }


def _validate() -> None:
    """Fail-loud at import on a malformed catalog (mirrors the repo's no-silent posture)."""
    for t in TIERS:
        unknown = t.features - set(FEATURE_BY_KEY)
        if unknown:
            raise ValueError(f"tier {t.key!r} references unknown feature(s): {sorted(unknown)}")
    # Tiers must nest cheapest → richest (each a superset of the prior).
    for prev, cur in zip(TIERS, TIERS[1:]):
        if not prev.features <= cur.features:
            raise ValueError(f"tier {cur.key!r} is not a superset of {prev.key!r}")
    for f in FEATURES:
        bad = set(f.requires) - ALL_SOURCES
        if bad:
            raise ValueError(f"feature {f.key!r} requires unknown source(s): {sorted(bad)}")


_validate()
