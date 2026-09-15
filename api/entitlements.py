"""Product-tier entitlements — the source of truth for what each Metron tier offers.

Drives the **tier simulator** (owner-only preview of Beta / Intelligence (demo)
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

from contextvars import ContextVar, Token
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
    # The glance screen (metron-ops#248) composes only data each tier may already see;
    # feed-derived zones degrade to "not available" inside the payload.
    Feature("glance", "Glance screen", ("broker",)),
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
    # Earnings calendar comes from the data-spine (yfinance-derived) → feed-gated, so it's
    # hidden in the no-feed beta rather than shown empty (metron-ops#52/#53).
    Feature("calendar", "Calendar (earnings)", ("feed",)),
    # Major-index intraday strip (SPY/QQQ/IWM proxies) on the Overview — the index/ETF
    # quotes come from the licensed feed, so it's Pro-only and locked in the no-feed beta.
    Feature("indices", "Market indices (intraday)", ("feed",)),
    # Market board (metron-ops-I304) — technical attractiveness across Held/Watchlist.
    # Nav-only entry: the router enforces the real gate directly via settings.feed_entitled
    # (api/routers/market_board.py), mirroring Deploy Cash — this key exists so the nav can
    # hide/lock the link the same way it does for every other feed-dependent page.
    Feature("market_board", "Market board (technical attractiveness)", ("feed",)),
    Feature("etf_lookthrough", "ETF look-through", ("etf_vendor",)),
    Feature("agentic_research", "Agentic quant research", ("feed",)),
    Feature("ai_advisor", "Intelligence", ()),
    Feature("alpha_engine", "Alpha Engine signals", ()),
    # Neutral research intel (regime + narrative, sector ratings/modifiers, market
    # breadth, per-holding attractiveness + generic thesis) read from the crucible-research
    # `research_intel/latest.json` artifact (EPIC config#1499 / metron-ops#117). Sibling of
    # ai_advisor/alpha_engine: paid AI-Advisor-tier packaging, tier-gated only (the artifact
    # read is fail-soft, so availability is a tier decision, not a data-provisioning one —
    # free/Beta sees the quant spine only, never this edge OUTPUT).
    Feature("research_intel", "Research intel (regime · sector ratings · attractiveness)", ()),
    # New cash to my targets + what-if purchase (metron-ops-I311) — the L1 comparable to
    # deploy_cash: arithmetic against a table the user typed in, no Metron-chosen names and
    # no score, so both ship in the base (beta) tier rather than being feed/Pro-gated.
    Feature("cash_to_targets", "New cash to my targets", ()),
    Feature("whatif_purchase", "What-if purchase", ()),
    # Retire-early goal facets (metron-ops-I316): progress/trajectory/drag arithmetic
    # against a USER-AUTHORED goal (target/date/contribution/withdrawal rate). Doctrine
    # layer-2 exemption — a user-typed number, not a suitability input — so this ships
    # in the no-feed beta like the other free-source features, not gated behind
    # ai_advisor/alpha_engine. Requires no data source: the ledger/broker sources it
    # depends on (performance, tax) are already _BETA-packaged.
    Feature("goal", "Retire-early goal", ()),
)
FEATURE_BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}


# ── Tiers (ordered cheapest → most complete; required_tier reads this order) ──
@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    features: frozenset[str]


# Feature layers. _BETA and _PERSONAL are the two EXPOSED product tiers (TIERS below);
# _PRO and _AGENTIC stay as internal composition blocks (not selectable) so the catalog's
# layering is preserved for when packaging re-expands.
_BETA = frozenset({
    "overview", "glance", "income", "transactions", "tax",
    "concentration", "performance", "macro", "fundamentals",
    "cash_to_targets", "whatif_purchase", "goal",
})
_PRO = _BETA | {
    "auto_sync", "benchmark", "risk", "attribution", "scenarios", "calendar", "etf_lookthrough",
    "indices", "market_board",
}
_AGENTIC = _PRO | {"agentic_research"}
_PERSONAL = _AGENTIC | {"ai_advisor", "alpha_engine", "research_intel"}

# Two exposed options for now (metron-ops): "Beta" — everything legally releasable to the
# public pre-SEC-approval (no advice) — and "Intelligence (demo)" — the full product, advice
# /signals included. required_tier reads this order (cheapest → richest), so any feature the
# beta tier excludes upsells to "Intelligence" — never to the internal Pro/Research layers.
TIERS: tuple[Tier, ...] = (
    Tier("beta", "Beta", _BETA),
    Tier("personal", "Intelligence (demo)", _PERSONAL),
)
TIER_BY_KEY: dict[str, Tier] = {t.key: t for t in TIERS}
TIER_ORDER: list[str] = [t.key for t in TIERS]


# ── Per-request pin (external user demo, metron-ops-I310) ────────────────────
# Advice-flavored features: never available to an external-demo session, whatever the
# deployment's tier. Deploy cash is not a catalog feature (its router gates it directly),
# so it is refused by route in api/services/external_demo.py instead.
ADVICE_FEATURES: frozenset[str] = frozenset({"market_board", "research_intel", "alpha_engine", "ai_advisor", "agentic_research"})
# Feed-derived L1 features: live for an external-demo session unless the fallback config
# (``external_demo_feed_features_locked``) re-locks them.
FEED_DERIVED_FEATURES: frozenset[str] = frozenset(
    {"benchmark", "risk", "attribution", "scenarios", "calendar", "indices", "etf_lookthrough"}
)
PINNED_TIER_KEY = "external_demo"


@dataclass(frozen=True)
class Pin:
    """A server-side entitlement pin: the exact feature set and feed state a request
    resolves against, overriding the deployment tier, the feed setting and the simulator."""

    features: frozenset[str]
    feed_enabled: bool


_request_pin: ContextVar[Pin | None] = ContextVar("metron_entitlement_pin", default=None)


def external_demo_features(*, feed_features_locked: bool) -> frozenset[str]:
    """The no-advice feature set: ``_PRO`` minus the advice features, optionally minus the
    feed-derived ones (the fallback)."""
    features = _PRO - ADVICE_FEATURES
    if feed_features_locked:
        features = features - FEED_DERIVED_FEATURES
    return frozenset(features)


def set_request_pin(pin: Pin | None) -> Token:
    return _request_pin.set(pin)


def reset_request_pin(token: Token) -> None:
    _request_pin.reset(token)


def current_pin() -> Pin | None:
    return _request_pin.get()


def provisioned_sources(feed_enabled: bool) -> frozenset[str]:
    """The data sources currently available — free always, licensed iff feed on. A pinned
    request uses the pin's feed state."""
    pin = _request_pin.get()
    if pin is not None:
        feed_enabled = pin.feed_enabled
    return FREE_SOURCES | (FEED_SOURCES if feed_enabled else frozenset())


def required_tier(feature_key: str) -> str | None:
    """The cheapest tier that includes the feature (for the upsell), or None."""
    for t in TIERS:
        if feature_key in t.features:
            return t.key
    return None


def effective_axes(
    *, default_tier: str, feed_entitled: bool, simulator: bool,
    preview_tier: str | None = None, preview_feed: bool | None = None,
) -> tuple[str, bool]:
    """The ``(tier, feed)`` a request should resolve against: this deployment's defaults,
    overridden by the owner tier-simulator preview ONLY when the simulator is on (never on
    the public product, so a normal caller can't re-scope its own entitlements). The
    canonical form of the override mirrored ad-hoc in ``GET /meta/entitlements`` and
    ``portfolios._effective_entitlement``."""
    tier, feed = default_tier, feed_entitled
    pin = _request_pin.get()
    if pin is not None:
        # A pinned session ignores preview headers entirely; ``resolve`` substitutes the
        # pinned feature set for whichever tier key is passed.
        return default_tier, pin.feed_enabled
    if simulator:
        if preview_tier is not None:
            tier = preview_tier
        if preview_feed is not None:
            feed = preview_feed
    return tier, feed


def feature_state(
    feature_key: str, *, default_tier: str, feed_entitled: bool, simulator: bool,
    preview_tier: str | None = None, preview_feed: bool | None = None,
) -> dict:
    """One feature's resolved entitlement dict (``available`` / ``reason`` /
    ``required_tier`` / …) for a request's effective axes. A bad preview tier falls back
    to the deployment default rather than raising."""
    tier, feed = effective_axes(
        default_tier=default_tier, feed_entitled=feed_entitled, simulator=simulator,
        preview_tier=preview_tier, preview_feed=preview_feed,
    )
    try:
        resolved = resolve(tier, feed_enabled=feed)
    except ValueError:
        resolved = resolve(default_tier, feed_enabled=feed_entitled)
    return next(f for f in resolved["features"] if f["key"] == feature_key)


def resolve(tier: str, *, feed_enabled: bool) -> dict:
    """Per-feature availability for ``tier`` under the feed toggle.

    ``available = in_tier AND computable``. ``reason`` is ``None`` when available,
    ``"tier"`` when the tier doesn't include it (upsell to ``required_tier``), else
    the first missing data source (``"feed"`` / ``"benchmark"`` / ``"etf_vendor"``).
    """
    pin = _request_pin.get()
    if pin is not None:
        # The single chokepoint for the external-demo pin: every call site (routers, the
        # insights registry, plugins) resolves through here, so the pinned set wins no
        # matter which tier or feed the caller derived from settings or headers.
        tier, feed_enabled, active = PINNED_TIER_KEY, pin.feed_enabled, pin.features
    elif tier not in TIER_BY_KEY:
        raise ValueError(f"unknown tier {tier!r}; known: {TIER_ORDER}")
    else:
        active = TIER_BY_KEY[tier].features
    prov = provisioned_sources(feed_enabled)
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
    for prev, cur in zip(TIERS, TIERS[1:], strict=False):
        if not prev.features <= cur.features:
            raise ValueError(f"tier {cur.key!r} is not a superset of {prev.key!r}")
    for f in FEATURES:
        bad = set(f.requires) - ALL_SOURCES
        if bad:
            raise ValueError(f"feature {f.key!r} requires unknown source(s): {sorted(bad)}")


_validate()
