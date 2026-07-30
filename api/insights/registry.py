"""The insight facet catalog — the candidate pool the ranker selects from.

Every observation Metron can make about a portfolio is registered here as a ``Facet``.
Surfaces (the glance screen's ranked zones, the Overview insights strip) select from
this catalog; they never hard-code a widget per observation, because a widget-per-facet
layout reproduces the dashboard one screen further in.

**Two orthogonal axes decide whether a facet is available**, exactly mirroring
``api.entitlements`` rather than restating it:

1. **Tier** — does the active product tier package the ``feature`` this facet belongs to?
2. **Data provisioning** — are the ``requires`` sources provisioned (the licensed-feed
   toggle flips the ``feed`` / ``benchmark`` / ``etf_vendor`` half)?

**The third axis is regulatory and is this module's own.** ``level`` is ``L1`` for an
impersonal factual observation — legal pre-registration — and ``L2`` for anything
directive. The boundary is not stylistic: *"your three largest positions are all one
sector"* is an observation; *"trim NVDA"* is a directive. L2 facets exist here so the
catalog is honest about what the product could say, and are filtered out of every
pre-registration surface by ``candidate_facets``.

Two rows carry a standing warning because they are the ones most likely to be built
past the line by accident:

- **``security_attractiveness`` (E8)** — a universal rating is legal as impersonal market
  intelligence. The *same* rating rendered next to your position, sorted by your weight,
  with a decile flag, is where the publisher exemption gets tested. **Rate the security,
  never the holding.**
- **``unheld_rated_names`` (H5)** — surfacing names *Metron* picked is L2 regardless of
  hedging. The L1-legal forms are H1–H4, where the **user authored the filter** and
  Metron reports what passed it.

Catalog rows follow ``business/product-positioning/metron.md`` §3d in the private
nous-ergon-ops repo; the ``notes`` field carries the §3d rationale where a row is
non-obvious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api import entitlements

# ── Regulatory level (positioning doc §3c) ───────────────────────────────────
LEVEL_L1 = "L1"  # impersonal factual observation — ships pre-registration
LEVEL_L2 = "L2"  # directive / recommendation — gated on the adviser-line answer
LEVELS: frozenset[str] = frozenset({LEVEL_L1, LEVEL_L2})

# ── Families (positioning doc §3d A–K) ───────────────────────────────────────
FAMILIES: tuple[str, ...] = (
    "structure",  # A — portfolio structure & exposure
    "performance",  # B — performance & attribution
    "risk",  # C — risk
    "movement",  # D — daily movement
    "security",  # E — held securities: fundamentals, valuation, technicals
    "tax",  # F — tax
    "events",  # G — events & calendar
    "unheld",  # H — securities NOT held (the compliance frontier)
    "market",  # I — market context
    "integrity",  # J — data integrity: insights about the numbers themselves
    "behaviour",  # K — behaviour & process
)


@dataclass(frozen=True)
class Facet:
    """One registered observation type.

    ``requires`` names ``api.entitlements`` source keys (data provisioning) and
    ``feature`` names an ``api.entitlements`` feature key (tier packaging). Both are
    validated against that module by the contract test rather than duplicated here —
    entitlements stays the single source of truth for what a source or feature *is*.

    ``surface`` is the route a rendered observation taps through to, relative to a
    portfolio (``"tax"`` → ``/portfolios/{id}/tax``). Every claim resolves to the page
    that computed it: one tap, never two.
    """

    key: str
    family: str
    label: str
    requires: tuple[str, ...]
    feature: str
    surface: str
    level: str = LEVEL_L1
    enabled: bool = True
    notes: str = field(default="", compare=False)


def _f(
    key: str,
    family: str,
    label: str,
    requires: tuple[str, ...],
    feature: str,
    surface: str,
    *,
    level: str = LEVEL_L1,
    enabled: bool = True,
    notes: str = "",
) -> Facet:
    return Facet(
        key=key,
        family=family,
        label=label,
        requires=requires,
        feature=feature,
        surface=surface,
        level=level,
        enabled=enabled,
        notes=notes,
    )


_BROKER = ("broker",)
_LEDGER = ("ledger",)
_LEDGER_BROKER = ("ledger", "broker")
_FEED = ("feed",)
_SNAP = ("broker", "snapshots")

# ── A. Portfolio structure & exposure ────────────────────────────────────────
_STRUCTURE: tuple[Facet, ...] = (
    _f("concentration_top_weight", "structure", "Top-position concentration", _BROKER, "concentration", "holdings"),
    _f("concentration_hhi", "structure", "Diversification (HHI)", _BROKER, "concentration", "holdings"),
    _f(
        "target_allocation_drift",
        "structure",
        "Drift vs your target allocation",
        _BROKER,
        "overview",
        "holdings",
        notes="User-authored rule — exempt from the suitability wall. Metron evaluates the number the user typed; it never suggests the target.",
    ),
    _f(
        "position_cap_breach",
        "structure",
        "Position above your own cap",
        _BROKER,
        "overview",
        "holdings",
        notes="Arithmetic against a user-authored max_single_position, not advice.",
    ),
    _f("sector_exposure_shift", "structure", "Sector exposure change", _BROKER, "overview", "holdings"),
    _f("geographic_exposure", "structure", "Geographic exposure", _BROKER, "overview", "holdings"),
    _f("currency_exposure", "structure", "Currency exposure and FX contribution", _BROKER, "overview", "holdings"),
    _f("asset_class_mix", "structure", "Asset-class mix", _BROKER, "overview", "holdings"),
    _f("factor_tilt_profile", "structure", "Factor tilts and their drift", _FEED, "risk", "risk"),
    _f("etf_overlap", "structure", "Duplicated exposure across funds", ("etf_vendor",), "etf_lookthrough", "holdings"),
    _f("effective_bets", "structure", "Effective number of independent bets", _FEED, "risk", "risk"),
    _f("cash_drag", "structure", "Uninvested cash", _BROKER, "overview", "holdings"),
    _f("asset_location", "structure", "Tax-inefficient assets in taxable accounts", _LEDGER_BROKER, "tax", "tax"),
    _f("issuer_across_accounts", "structure", "Same issuer held in several accounts", _BROKER, "concentration", "holdings"),
)

# ── B. Performance & attribution ─────────────────────────────────────────────
_PERFORMANCE: tuple[Facet, ...] = (
    _f(
        "twr_mwr_gap",
        "performance",
        "What your timing cost (TWR vs MWR)",
        _SNAP,
        "performance",
        "performance",
        notes="The Sharesight counter-position: show both, explain the gap.",
    ),
    _f("brinson_attribution", "performance", "Allocation vs selection vs interaction", _FEED, "attribution", "attribution"),
    _f("benchmark_alpha", "performance", "Benchmark-relative alpha", ("benchmark",), "benchmark", "performance"),
    _f("top_contributors", "performance", "Top contributors and detractors", _BROKER, "performance", "performance"),
    _f("return_concentration", "performance", "How few positions drove the return", _BROKER, "performance", "performance"),
    _f("realized_unrealized_split", "performance", "Realized vs unrealized split", _LEDGER_BROKER, "tax", "tax"),
    _f("leadership_rotation", "performance", "Which holdings now drive the return", _SNAP, "performance", "performance"),
    _f("position_vs_sector", "performance", "Position return vs its sector", _FEED, "performance", "performance"),
    _f("income_yield_on_cost", "performance", "Yield on cost and income growth", _LEDGER, "income", "income"),
    _f("income_concentration", "performance", "Income concentration", _LEDGER, "income", "income"),
    _f("fee_drag", "performance", "Blended fund expense, in dollars", _BROKER, "overview", "holdings"),
)

# ── C. Risk ──────────────────────────────────────────────────────────────────
_RISK: tuple[Facet, ...] = (
    _f("volatility_beta", "risk", "Volatility and beta", _FEED, "risk", "risk"),
    _f("capture_ratios", "risk", "Upside and downside capture", _FEED, "risk", "risk"),
    _f("drawdown_state", "risk", "Drawdown and time under water", _SNAP, "performance", "performance"),
    _f("idiosyncratic_share", "risk", "Idiosyncratic vs systematic risk", _FEED, "risk", "risk"),
    _f("marginal_risk_contribution", "risk", "Risk out of proportion to weight", _FEED, "risk", "risk"),
    _f("scenario_replay", "risk", "Historical replay and factor shocks", _FEED, "scenarios", "risk"),
    _f("position_liquidity", "risk", "Days to liquidate a position", _FEED, "risk", "risk"),
    _f("event_risk_clustering", "risk", "Large positions reporting the same week", _FEED, "calendar", "calendar"),
    _f("correlation_regime", "risk", "When diversification stops working", _FEED, "risk", "risk"),
)

# ── D. Daily movement ────────────────────────────────────────────────────────
_MOVEMENT: tuple[Facet, ...] = (
    _f("movement_decomposition", "movement", "What moved the portfolio today", _BROKER, "overview", "overview"),
    _f("move_vs_expectation", "movement", "Moved more or less than its beta implied", _FEED, "risk", "risk"),
    _f("outlier_move", "movement", "A position moved beyond its own history", _FEED, "risk", "holdings"),
    _f(
        "valuation_provenance_delta",
        "movement",
        "Settled vs intraday vs as-of-close",
        _BROKER,
        "overview",
        "overview",
        notes="The provenance badge doing load-bearing work — see positioning §3g.4 law 4.",
    ),
    _f("new_highs_lows", "movement", "New highs and lows within holdings", _FEED, "risk", "holdings"),
    _f("intraday_sector_rotation", "movement", "Sector rotation inside the portfolio", _FEED, "risk", "holdings"),
)

# ── E. Held securities ───────────────────────────────────────────────────────
_SECURITY: tuple[Facet, ...] = (
    _f("valuation_vs_history", "security", "Valuation vs its own history and sector", _FEED, "fundamentals", "tearsheet"),
    _f("fundamental_trend", "security", "Revenue, margin and FCF direction", ("edgar",), "fundamentals", "tearsheet"),
    _f("leverage_change", "security", "Balance-sheet and leverage change", ("edgar",), "fundamentals", "tearsheet"),
    _f("share_count_change", "security", "Dilution or buyback", ("edgar",), "fundamentals", "tearsheet"),
    _f("earnings_surprise_history", "security", "Earnings surprises and next report", _FEED, "calendar", "calendar"),
    _f("technical_state", "security", "Moving averages and 52-week extremes", _FEED, "fundamentals", "tearsheet"),
    _f(
        "narrative_read",
        "security",
        "Narrative and sentiment read",
        _FEED,
        "research_intel",
        "research-intel",
        notes="research_intel is not live-refreshed — an as-of date is mandatory next to the claim (doctrine, staleness).",
    ),
    _f(
        "security_attractiveness",
        "security",
        "Universal attractiveness rating",
        _FEED,
        "research_intel",
        "research-intel",
        notes="HIGHEST-RISK L1 row. Rate the SECURITY, never the holding — a universal rating sorted by your weight with a decile flag is where the publisher exemption gets tested.",
    ),
    _f(
        "held_buy_sell_signal",
        "security",
        "Buy/sell signal on a held name",
        _FEED,
        "alpha_engine",
        "alpha-engine",
        level=LEVEL_L2,
        notes="Directive by construction — L2.",
    ),
)

# ── F. Tax ───────────────────────────────────────────────────────────────────
_TAX: tuple[Facet, ...] = (
    _f("unrealized_by_lot", "tax", "Unrealized gain/loss by lot", _LEDGER_BROKER, "tax", "tax"),
    _f("long_term_boundary", "tax", "Lots approaching the one-year boundary", _LEDGER_BROKER, "tax", "tax"),
    _f(
        "harvestable_losses_present",
        "tax",
        "Positions currently held at a loss",
        _LEDGER_BROKER,
        "tax",
        "tax",
        notes="Descriptive only. Stating that losses exist is an observation; naming which to sell is F8 and is L2.",
    ),
    _f("wash_sale_window", "tax", "A sale within 30 days of a repurchase", _LEDGER, "tax", "tax"),
    _f("ytd_realized_vs_prior", "tax", "YTD realized gains vs last year", _LEDGER, "tax", "tax"),
    _f(
        "if_sold_tax_math",
        "tax",
        "Tax math on a hypothetical you chose",
        _LEDGER_BROKER,
        "tax",
        "tax",
        notes="L1 ONLY because the user authors the hypothetical. Metron must never pre-select the position (metron-ops#208).",
    ),
    _f("dividend_qualification", "tax", "Qualified dividends and withholding", _LEDGER, "tax", "tax"),
    _f(
        "recommended_harvest_set",
        "tax",
        "Which losses to harvest",
        _LEDGER_BROKER,
        "ai_advisor",
        "intelligence",
        level=LEVEL_L2,
        notes="Naming which to sell is a directive — L2.",
    ),
)

# ── G. Events & calendar ─────────────────────────────────────────────────────
_EVENTS: tuple[Facet, ...] = (
    _f("upcoming_earnings", "events", "Upcoming earnings for your holdings", _FEED, "calendar", "calendar"),
    _f("ex_dividend_ahead", "events", "Approaching ex-dividend dates", _LEDGER, "income", "income"),
    _f("corporate_actions", "events", "Splits, M&A and index changes", _FEED, "calendar", "calendar"),
    _f("macro_print_sensitivity", "events", "A macro print your portfolio is sensitive to", ("fred_pubdomain", "feed"), "macro", "macro"),
    _f("cashflow_effect_on_mwr", "events", "How a contribution moved your MWR", _LEDGER, "performance", "performance"),
)

# ── H. Securities NOT held — the compliance frontier ─────────────────────────
_UNHELD: tuple[Facet, ...] = (
    _f("watchlist_movement", "unheld", "Watchlist movement", _BROKER, "overview", "watchlist"),
    _f("sector_peer_divergence", "unheld", "A sector peer diverged from your holding", _FEED, "performance", "performance"),
    _f(
        "screener_entries",
        "unheld",
        "Names entering your own screener",
        _FEED,
        "research_intel",
        "research-intel",
        notes="L1 because the USER authored the filter and Metron reports what passed it (metron-ops#169).",
    ),
    _f(
        "target_exposure_gap",
        "unheld",
        "A gap against your target allocation",
        _BROKER,
        "overview",
        "holdings",
        notes="User-authored target — arithmetic, not advice.",
    ),
    _f(
        "unheld_rated_names",
        "unheld",
        "Unheld names Metron rates attractive",
        _FEED,
        "ai_advisor",
        "intelligence",
        level=LEVEL_L2,
        notes="THE facet most likely to be built by accident. A ranked list of names to consider is a recommendation regardless of hedging — Metron chose the names, so L2.",
    ),
)

# ── I. Market context ────────────────────────────────────────────────────────
_MARKET: tuple[Facet, ...] = (
    _f("regime_read", "market", "Market regime and narrative", _FEED, "research_intel", "research-intel"),
    _f("market_breadth", "market", "Market breadth", _FEED, "research_intel", "research-intel"),
    _f("index_sector_performance", "market", "Index and sector performance", _FEED, "indices", "overview"),
    _f("rates_and_prints", "market", "Rates, curve and inflation prints", ("fred_pubdomain",), "macro", "macro"),
    _f("volatility_regime", "market", "Volatility regime", _FEED, "risk", "risk"),
    _f(
        "market_context_for_your_exposure",
        "market",
        "Today's market move, filtered to what you own",
        _FEED,
        "risk",
        "risk",
        notes="The only non-commodity row in this family — market context only a portfolio-analytics product can state.",
    ),
)

# ── J. Data integrity ────────────────────────────────────────────────────────
_INTEGRITY: tuple[Facet, ...] = (
    _f(
        "reconciliation_status",
        "integrity",
        "Reconciled against your custodian",
        _BROKER,
        "overview",
        "diagnostics",
        notes="Rendered on the HEALTHY path too — an integrity signal that appears only on failure trains the user to read blank as good.",
    ),
    _f("sync_staleness", "integrity", "How current your positions are", _BROKER, "overview", "diagnostics"),
    _f("missing_cost_basis", "integrity", "Holdings missing cost basis", _LEDGER_BROKER, "tax", "diagnostics"),
    _f("unclassified_holdings", "integrity", "Holdings Metron could not classify", _BROKER, "overview", "diagnostics"),
)

# ── K. Behaviour & process ───────────────────────────────────────────────────
_BEHAVIOUR: tuple[Facet, ...] = (
    _f("timing_cost", "behaviour", "What your contribution timing cost", _SNAP, "performance", "performance"),
    _f("turnover_cost", "behaviour", "Turnover and its tax cost", _LEDGER, "transactions", "transactions"),
    _f("contribution_consistency", "behaviour", "Contribution consistency", _LEDGER, "transactions", "transactions"),
    _f(
        "insight_outcome_history",
        "behaviour",
        "What previous insights said, and what happened",
        _BROKER,
        "overview",
        "overview",
        enabled=False,
        notes="Depends on the insight ledger (metron-ops#253). Registered disabled so the catalog is honest about it rather than silently missing it.",
    ),
)

CATALOG: tuple[Facet, ...] = (
    *_STRUCTURE,
    *_PERFORMANCE,
    *_RISK,
    *_MOVEMENT,
    *_SECURITY,
    *_TAX,
    *_EVENTS,
    *_UNHELD,
    *_MARKET,
    *_INTEGRITY,
    *_BEHAVIOUR,
)

FACET_BY_KEY: dict[str, Facet] = {f.key: f for f in CATALOG}


def facets_in_family(family: str) -> tuple[Facet, ...]:
    """Every registered facet in ``family``, catalog order preserved."""
    return tuple(f for f in CATALOG if f.family == family)


def candidate_facets(
    tier: str,
    *,
    feed_enabled: bool,
    max_level: str = LEVEL_L1,
) -> tuple[Facet, ...]:
    """The facets a surface may draw on for ``tier`` under the feed toggle.

    A facet survives only when **all four** hold:

    1. it is ``enabled`` (a registered-but-unbuilt facet never reaches a surface);
    2. its ``level`` is within ``max_level`` — ``L1`` excludes every directive facet,
       which is what makes a pre-registration surface safe by construction rather than
       by the caller remembering to filter;
    3. its ``feature`` is packaged in ``tier``;
    4. every source in ``requires`` is provisioned.

    Conditions 3 and 4 are delegated to ``api.entitlements`` rather than re-derived, so
    a packaging change there cannot silently disagree with this module. Filtering
    happens **before** ranking: a facet the deployment cannot compute never enters the
    candidate pool, rather than being ranked and then hidden.
    """
    if max_level not in LEVELS:
        raise ValueError(f"unknown level {max_level!r}; known: {sorted(LEVELS)}")
    resolved = entitlements.resolve(tier, feed_enabled=feed_enabled)
    in_tier = {f["key"] for f in resolved["features"] if f["in_tier"]}
    provisioned = entitlements.provisioned_sources(feed_enabled)
    allowed_levels = {LEVEL_L1} if max_level == LEVEL_L1 else LEVELS
    return tuple(
        f
        for f in CATALOG
        if f.enabled
        and f.level in allowed_levels
        and f.feature in in_tier
        and all(r in provisioned for r in f.requires)
    )
