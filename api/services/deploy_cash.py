"""Deploy cash — "if I have $x to spend today, what should I spend it on?" (metron-ops#300,
Brian ruling 2026-09-14).

A **ranking under constraints**, never a forecast. The technical rating this ranks on graded
IC ~= 0 at 1-20 days (metron-ops#295), so nothing in this module claims predictive edge: it
orders the candidates the user already holds or watches by the rating, then spends the cash
down that order under declared position/sector/line-size limits, and reports every dollar it
could not place and why. The rating's own track record is published beside it
(``technical_rating_performance.py``) and linked from the panel.

Owner (feed-entitled) build only, exactly like the rating itself — the no-feed beta never
reaches this module (the router 404s before it is imported into a request), so no
yfinance-derived datum can leak onto a beta surface (metron-ops#52).

**Every input is a collector artifact or the user's own Metron DB** — this module makes no
vendor fetch of any kind (foundation rule, ``architecture.d/146``):

- technical rating + as-of — ``api.services.technical_rating`` (intraday artifact where
  fresh, EOD ``technicals/latest.json`` fallback, decided per symbol);
- prices — the cached EOD closes (``api.services.prices``) with the intraday overlay
  (``api.services.intraday.live_prices``) on top, same freshness semantics as Holdings;
- sectors — the data spine, via ``api.services.sectors`` (+ tenant overrides);
- holdings, watchlist and cash — the Metron DB.

A v2 champion prediction (``predictions/{trading_day}.json``) is a **future, optional and
labelled** extra input. It is deliberately not a dependency of anything here: this feature
works, and is complete, without one.

## The ranking rule is a champion (champion-challenger-policy §2, §9)

Ranking candidates is a swappable decision rule, so it is registered as a slot rather than
hard-coded at the call site. ``RANKERS`` is the arm register, ``LIVE_CHAMPION`` names the
serving arm, and ``SLOT`` carries the slot's measurement contract and ``ArenaConfig``
parameters (§10 requires a slot to name them where CI can see them). The serving path
resolves ``RANKERS[LIVE_CHAMPION]`` and never imports a ranking function directly (§4) — so
adding a challenger is a register row plus a scoring path, not a rewrite.

Today the slot runs one arm, bootstrap-promoted with no prior cohort (§9.1) and with no
challenger yet (§9.2). §9.2 still requires the champion to be *measured*: it is, by the
rating track record this ranks on (``market_data/technicals/rating_performance.json``),
which is rendered next to its own noise floor on the Diagnostics page.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from api.services import classifications as classifications_service
from api.services import intraday as intraday_service
from api.services import prices as price_service
from api.services import sectors as sectors_service
from api.services import technical_rating as technical_rating_service

# Verbatim from metron-ops#300 — the panel renders this string unchanged. It says what the
# ranking IS (an ordering under limits, off recent price action) and what it is not.
DISCLAIMER = (
    "Ranked by technical attractiveness under your position and sector limits. "
    "Describes recent price action; not investment advice."
)


# ── The constraint config block (one place, declared defaults) ────────────────
@dataclass(frozen=True)
class DeployCashConfig:
    """Every constraint the plan applies, in one block with its defaults (metron-ops#300).

    Deliberately a frozen dataclass rather than scattered literals: the plan echoes this
    block back to the caller, so the UI renders the limits it was actually run under and a
    future per-user override has exactly one place to land.
    """

    # Cap on any one position's weight AFTER the purchase, as a fraction of the
    # post-deployment book (see ``_denominator``).
    max_position_weight: float = 0.10
    # Cap on any one GICS sector's weight after the purchase, same denominator.
    max_sector_weight: float = 0.30
    # A line smaller than this is not worth placing — it is dropped, not shrunk.
    min_line_usd: float = 500.0
    # Fractional shares are not assumed to be available; a line is floor()ed to shares.
    whole_shares_only: bool = True
    # Only these rating labels are eligible. Neutral / Sell / Strong Sell are skipped —
    # this plan never proposes buying something the rating does not rate positively.
    eligible_labels: tuple[str, ...] = ("Buy", "Strong Buy")


DEFAULT_CONFIG = DeployCashConfig()


# ── Candidates, lines, plan ───────────────────────────────────────────────────
@dataclass
class Candidate:
    """One priced, rated buy candidate — a holding or a watchlist entry."""

    ticker: str
    price: float
    price_as_of: date | None
    price_source: str
    score: float
    label: str
    rating_basis: str
    rating_as_of: str | None
    sector: str | None
    existing_mv: float
    existing_weight: float
    held: bool
    ma_score: float | None = None
    osc_score: float | None = None
    n_votes: int | None = None


@dataclass
class CandidateSet:
    """Everything one read of the book + the spine yields, so ``recommend`` reads each of
    them exactly once (``valued_holdings`` and the sector resolution are both real queries)."""

    candidates: list[Candidate]
    skipped: list[dict]
    portfolio_value: float
    base_currency: str
    rating_as_of: str | None
    rating_basis: str | None
    # Today's market value per GICS sector, from the held book — the sector cap's running
    # total is seeded from this.
    sector_mv: dict[str, float]


@dataclass
class DeployCashLine:
    ticker: str
    usd: float
    shares_est: float
    price: float
    price_as_of: date | None
    technical_label: str
    score: float
    reasons: list[str] = field(default_factory=list)
    constraints_hit: list[str] = field(default_factory=list)


@dataclass
class DeployCashPlan:
    as_of: date
    amount_usd: float
    allocated_usd: float
    unallocated_usd: float
    unallocated_reasons: list[str]
    lines: list[DeployCashLine]
    # The book the weights are measured against: priced market value today, plus the cash
    # being deployed (see ``_denominator``).
    portfolio_value: float
    deployment_basis: float
    base_currency: str
    rating_as_of: str | None
    rating_basis: str | None
    config: DeployCashConfig
    champion: str
    disclaimer: str = DISCLAIMER
    # Candidates considered but never given a line, with the binding reason. Reported so
    # "why isn't X here?" is answerable from the artifact rather than from the code.
    skipped: list[dict] = field(default_factory=list)


# ── The ranking slot: register, champion pointer, measurement contract ────────
# A ranking function takes the candidate set and the config and returns it ordered
# best-first. It ranks ONLY — it never applies a constraint, which keeps the thing under
# test (the ordering) separable from the thing that is fixed (the limits).
Ranker = Callable[[list[Candidate], DeployCashConfig], list[Candidate]]


@dataclass(frozen=True)
class RankerSpec:
    key: str
    label: str
    rank: Ranker
    # Immutable recipe fields (champion-challenger-policy §3.1): what this arm IS, so a
    # later arm can be compared against a fixed description rather than against whatever
    # the code has drifted into.
    inputs: tuple[str, ...]
    description: str


def _rank_tech_score_desc(candidates: list[Candidate], config: DeployCashConfig) -> list[Candidate]:
    """Champion arm ``tech_score_desc_v1``: technical score descending; ties broken by the
    LOWER existing weight (so a tie goes to the name the book is least exposed to), then by
    ticker so the ordering is total and deterministic for the golden test."""
    return sorted(candidates, key=lambda c: (-c.score, c.existing_weight, c.ticker))


RANKERS: dict[str, RankerSpec] = {
    "tech_score_desc_v1": RankerSpec(
        key="tech_score_desc_v1",
        label="Technical score, descending",
        rank=_rank_tech_score_desc,
        inputs=("market_data/technicals/latest.json::rating", "market_data/intraday/technical_ratings.json"),
        description=(
            "Order by the composite MA+oscillator technical score, highest first; ties to the "
            "lower existing portfolio weight, then ticker."
        ),
    ),
}

# The serving pointer. The serving path resolves this from the register and never imports a
# ranking function directly (champion-challenger-policy §4) — ``test_deploy_cash`` locks it.
LIVE_CHAMPION = "tech_score_desc_v1"

# The slot's measurement contract + ArenaConfig parameters. §10 requires these to live where
# CI can read them against the code rather than in prose. Defaults are the fleet defaults;
# this slot declares no delta.
SLOT: dict = {
    "slot": "metron.deploy_cash.ranker",
    "champion": LIVE_CHAMPION,
    # §9.1 — bootstrap promotion without a prior cohort: the slot must serve something for
    # the feature to exist at all. Recorded as bootstrap, not as an evidence win.
    "promotion_source": "operator_bootstrap",
    "promoted_at": "2026-09-14",
    # §9.2 — one plausible rule today, so no challenger is registered yet. The champion is
    # still measured: the rating's realized track record (rating_performance.json) is the
    # slot's yardstick and is rendered against its own noise floor on Diagnostics.
    "challengers": [],
    "metric": "mean forward excess return of the ranked lines vs the candidate population",
    # §4: a selection-stage slot is graded against the population it drew from. Grading it
    # against a market index would measure that population's luck rather than the arm's
    # ordering, which inverted wins and losses outright when it was tried (2026-08-17).
    "benchmark": "the candidate population the arm ranked (holdings union watchlist)",
    "count_match": "equal number of lines per arm on the same as_of and the same candidate set",
    "arena_config": {
        "alpha": 0.05,
        "diff_clip": 0.10,
        "cap": 5,
        "grace_weeks": 4,
        "promote_min_weeks": 4,
        "promote_evidence": "anytime_valid",
        "min_active_arms": 3,
        "retired_trailing_cycles": 4,
        "retire_evidence": "point",
    },
    # Measured 2026-09-14 (metron-ops#295): IC ~= -0.017 at 5d. The champion is served
    # because an ordering is needed, not because it is known to work — which is exactly why
    # the surface presents it as a ranking and links its track record.
    "known_edge": "none measured at 1-20d (metron-ops#295)",
}


def live_ranker() -> RankerSpec:
    """The serving arm, resolved from the register (never a direct import)."""
    return RANKERS[LIVE_CHAMPION]


# ── Plan construction ─────────────────────────────────────────────────────────
def _denominator(portfolio_value: float, amount_usd: float) -> float:
    """The book every weight in this plan is measured against: today's priced market value
    plus the whole amount being deployed.

    Fixed up front, deliberately. Measuring each line against a denominator that grows as
    earlier lines are placed would make a line's cap depend on how many lines preceded it,
    so the same candidate set would produce different limits depending on iteration order —
    a plan nobody can reason about. A constant denominator means "10% max position weight"
    means the same thing for the first line and the last.
    """
    return portfolio_value + amount_usd


def _resolve_sectors(
    session: Session, tenant_id: uuid.UUID, symbols: list[str]
) -> dict[str, str | None]:
    """Sector per symbol, resolved exactly as the Holdings table resolves it (data-spine
    reference data, tenant overrides winning) so a name classifies the same here as there."""
    if not symbols:
        return {}
    sectors_service.ensure_sectors(session, symbols)
    base = sectors_service.sectors_by_symbol(session, symbols)
    overrides = classifications_service.overrides_by_symbol(session, tenant_id, symbols)
    out: dict[str, str | None] = {}
    for sym in symbols:
        ov = overrides.get(sym)
        out[sym] = (ov.sector if ov and ov.sector else None) or base.get(sym)
    return out


def _watchlist_symbols(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[str]:
    """Watchlist tickers, read straight off the table.

    Deliberately NOT ``watchlist.list_watchlist``: that builds the full comparison row
    (fundamentals, consensus, attractiveness, …) for every entry, none of which this plan
    reads. The symbols are all that is needed, and the rating/price/sector legs below are
    the same ones a holding gets.
    """
    rows = session.scalars(
        select(models.WatchlistItem.symbol).where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
        )
    ).all()
    return sorted({str(s) for s in rows if s})


def _skip(skipped: list[dict], ticker: str, reason: str, detail: str) -> None:
    skipped.append({"ticker": ticker, "reason": reason, "detail": detail})


def build_candidates(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    feed_entitled: bool,
    config: DeployCashConfig = DEFAULT_CONFIG,
    ratings=None,
    price_reader=None,
    now=None,
) -> CandidateSet:
    """The priced, rated, eligible candidate set plus the book it is measured against.

    ``ratings`` / ``price_reader`` / ``now`` are injectable test seams, mirroring every
    other data-spine consumer in this package.
    """
    skipped: list[dict] = []
    held = analytics.valued_holdings(session, tenant_id, portfolio_id)
    base_currency = analytics._base_currency(session, portfolio_id)

    # The book: priced market value in base currency. Unpriced holdings contribute nothing
    # (they are also not candidates) — never a fabricated valuation.
    portfolio_value = sum(h.market_value for h in held if h.market_value is not None)

    held_by_ticker = {h.ticker: h for h in held if h.ticker}
    watch = [s for s in _watchlist_symbols(session, tenant_id, portfolio_id) if s not in held_by_ticker]
    universe = sorted(set(held_by_ticker) | set(watch))

    # Today's sector exposure, from the held book — the running total the sector cap counts
    # against. Resolved over the whole universe in one pass (candidates need it too).
    sector_of = _resolve_sectors(session, tenant_id, universe)
    sector_mv: dict[str, float] = {}
    for h in held:
        sec = sector_of.get(h.ticker)
        if sec and h.market_value is not None:
            sector_mv[sec] = sector_mv.get(sec, 0.0) + h.market_value

    if not universe:
        return CandidateSet([], skipped, portfolio_value, base_currency, None, None, sector_mv)

    # Ratings — the whole snapshot, keyed by yf_symbol. Metron's tickers are yf symbols on
    # the US book this serves; an unmatched ticker is simply un-rated, never guessed.
    snapshot = ratings if ratings is not None else technical_rating_service.load_technical_rating()
    by_symbol = snapshot.by_symbol

    # Prices: cached EOD closes with the intraday overlay on top where it applies, i.e. the
    # SAME price the Holdings live view shows. ``live_prices`` returns None when the overlay
    # doesn't apply (no feed / stale / no usable quote) — then EOD close alone, exactly as
    # the settled view values.
    # A held ticker resolves by the currency it is actually held under (metron-I399). A
    # watchlist symbol resolves through the tenant's own Security links (e.g. a closed-out
    # position); only one the tenant has never linked falls back to the global Security
    # lookup (metron-ops#351).
    watch_ccy = analytics._tenant_currency_by_symbol(session, tenant_id, watch)
    currency_by_symbol = analytics._currency_by_symbol(session, [s for s in watch if s not in watch_ccy])
    currency_by_symbol.update(watch_ccy)
    currency_by_symbol.update({t: h.currency for t, h in held_by_ticker.items() if h.currency})
    eod = price_service.latest_close_by_symbol(session, universe, currency_by_symbol=currency_by_symbol)
    overlay, meta = intraday_service.live_prices(
        session, universe, feed_entitled=feed_entitled, currency_by_symbol=currency_by_symbol,
        reader=price_reader, now=now,
    )
    priced = overlay if overlay else eod
    price_source = "intraday" if overlay else "eod_close"

    rating_as_of: str | None = None
    rating_basis: str | None = None
    candidates: list[Candidate] = []
    for ticker in universe:
        rating = by_symbol.get(ticker)
        if rating is None or rating.score is None or rating.label is None:
            _skip(skipped, ticker, "no_rating", "no technical rating published for this symbol")
            continue
        if rating.label not in config.eligible_labels:
            _skip(skipped, ticker, "label", f"rated {rating.label} — only {', '.join(config.eligible_labels)} are eligible")
            continue
        point = priced.get(ticker)
        if point is None or not point.close or point.close <= 0:
            _skip(skipped, ticker, "unpriced", "no cached close or live quote for this symbol")
            continue
        currency = currency_by_symbol.get(ticker, "USD")
        if currency != base_currency:
            # The plan spends one pot of cash in the portfolio's base currency. A foreign
            # listing would need an FX leg to size a line honestly, so it is excluded and
            # SAID SO, rather than silently priced 1:1.
            _skip(skipped, ticker, "currency", f"priced in {currency}, not the {base_currency} deployment currency")
            continue
        sector = sector_of.get(ticker)
        if sector is None:
            # The sector cap cannot be evaluated for an unclassified name, and quietly
            # exempting it from the cap would be the cap silently not applying.
            _skip(skipped, ticker, "sector_unknown", "no sector classification — the sector limit can't be evaluated")
            continue
        existing = held_by_ticker.get(ticker)
        existing_mv = (existing.market_value or 0.0) if existing is not None else 0.0
        candidates.append(
            Candidate(
                ticker=ticker,
                price=float(point.close),
                price_as_of=getattr(point, "bar_date", None),
                price_source=price_source,
                score=float(rating.score),
                label=rating.label,
                rating_basis=rating.basis,
                rating_as_of=rating.as_of,
                sector=sector,
                existing_mv=existing_mv,
                existing_weight=(existing_mv / portfolio_value) if portfolio_value > 0 else 0.0,
                held=existing is not None,
                ma_score=rating.ma_score,
                osc_score=rating.osc_score,
                n_votes=rating.n_votes,
            )
        )
        if rating_as_of is None:
            rating_as_of, rating_basis = rating.as_of, rating.basis

    # Report the overlay's own status where it refused to apply, so a plan priced off
    # yesterday's close during a session says why.
    if not overlay and meta.reason:
        _skip(skipped, "*", "price_basis", f"live quotes not applied ({meta.reason}); priced from the last cached close")

    return CandidateSet(candidates, skipped, portfolio_value, base_currency, rating_as_of, rating_basis, sector_mv)


def recommend(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    amount_usd: float,
    *,
    as_of: date | None = None,
    feed_entitled: bool,
    config: DeployCashConfig = DEFAULT_CONFIG,
    ratings=None,
    price_reader=None,
    now=None,
) -> DeployCashPlan:
    """Rank ``amount_usd`` of cash across the portfolio's candidates under ``config``.

    A ranking under constraints, not a forecast (metron-ops#295): the order comes from the
    slot champion, the sizing comes from the declared limits, and every dollar that could
    not be placed is reported with the reason it could not be.

    Greedy down the ranked order, which is the correct shape here: the constraints are
    per-name and per-sector caps against a FIXED denominator, so taking each candidate to
    its cap in rank order yields the same allocation any exhaustive search would under this
    objective — there is nothing for a later line to trade against an earlier one.
    """
    as_of = as_of or date.today()
    amount_usd = max(0.0, float(amount_usd))

    cset = build_candidates(
        session, tenant_id, portfolio_id,
        feed_entitled=feed_entitled, config=config, ratings=ratings, price_reader=price_reader, now=now,
    )
    skipped = cset.skipped

    basis = _denominator(cset.portfolio_value, amount_usd)
    ranked = live_ranker().rank(cset.candidates, config)

    # Running exposure, seeded from today's book so a cap counts what is already owned.
    mv_by_ticker = {c.ticker: c.existing_mv for c in ranked}
    sector_mv = dict(cset.sector_mv)

    remaining = amount_usd
    lines: list[DeployCashLine] = []
    for c in ranked:
        if remaining < config.min_line_usd:
            _skip(skipped, c.ticker, "cash_exhausted", f"only {remaining:,.2f} left, below the {config.min_line_usd:,.0f} minimum line")
            continue
        constraints_hit: list[str] = []

        pos_cap = config.max_position_weight * basis
        pos_headroom = pos_cap - mv_by_ticker.get(c.ticker, 0.0)
        sec_cap = config.max_sector_weight * basis
        sec_headroom = sec_cap - sector_mv.get(c.sector, 0.0)

        budget = remaining
        if pos_headroom < budget:
            budget = pos_headroom
            constraints_hit.append("max_position_weight")
        if sec_headroom < budget:
            budget = sec_headroom
            constraints_hit.append("max_sector_weight")

        if budget <= 0:
            reason = "max_position_weight" if pos_headroom <= 0 else "max_sector_weight"
            _skip(skipped, c.ticker, reason, f"already at or above the {reason.replace('_', ' ')} limit")
            continue

        shares = math.floor(budget / c.price) if config.whole_shares_only else budget / c.price
        usd = round(shares * c.price, 2)
        if shares <= 0 or usd < config.min_line_usd:
            _skip(
                skipped, c.ticker, "min_line_usd",
                f"the largest line the limits allow is {usd:,.2f}, below the {config.min_line_usd:,.0f} minimum",
            )
            if config.whole_shares_only and shares <= 0:
                skipped[-1]["detail"] = f"one share costs {c.price:,.2f} — more than the {budget:,.2f} the limits allow"
            continue

        weight_before = (mv_by_ticker.get(c.ticker, 0.0)) / basis if basis > 0 else 0.0
        weight_after = (mv_by_ticker.get(c.ticker, 0.0) + usd) / basis if basis > 0 else 0.0
        sector_after = (sector_mv.get(c.sector, 0.0) + usd) / basis if basis > 0 else 0.0

        reasons = [
            f"Technical rating {c.label} (score {c.score:+.2f}, {c.rating_basis} basis)",
            f"MA {c.ma_score:+.2f} · oscillator {c.osc_score:+.2f}"
            if c.ma_score is not None and c.osc_score is not None
            else "MA / oscillator sub-scores not published",
            f"Weight {weight_before * 100:.1f}% → {weight_after * 100:.1f}% (limit {config.max_position_weight * 100:.0f}%)",
            f"{c.sector} {sector_mv.get(c.sector, 0.0) / basis * 100 if basis > 0 else 0:.1f}% → {sector_after * 100:.1f}% "
            f"(limit {config.max_sector_weight * 100:.0f}%)",
            "already held" if c.held else "on your watchlist, not held",
        ]
        if c.n_votes is not None:
            reasons.append(f"{c.n_votes} indicator votes")

        lines.append(
            DeployCashLine(
                ticker=c.ticker, usd=usd, shares_est=shares, price=c.price, price_as_of=c.price_as_of,
                technical_label=c.label, score=c.score, reasons=reasons, constraints_hit=constraints_hit,
            )
        )
        mv_by_ticker[c.ticker] = mv_by_ticker.get(c.ticker, 0.0) + usd
        sector_mv[c.sector] = sector_mv.get(c.sector, 0.0) + usd
        remaining = round(remaining - usd, 2)

    allocated = round(sum(line.usd for line in lines), 2)
    unallocated = round(amount_usd - allocated, 2)

    # Unallocated cash is REPORTED, never forced into a line (metron-ops#300). The reasons
    # are the binding constraints, deduped, in the order they bound.
    unallocated_reasons: list[str] = []
    if unallocated > 0:
        seen: set[str] = set()
        for row in skipped:
            key = str(row["reason"])
            if key in ("price_basis",) or key in seen:
                continue
            seen.add(key)
            unallocated_reasons.append(_UNALLOCATED_REASON_TEXT.get(key, key))
        if not unallocated_reasons:
            unallocated_reasons.append(_UNALLOCATED_REASON_TEXT["min_line_usd"])

    return DeployCashPlan(
        as_of=as_of,
        amount_usd=round(amount_usd, 2),
        allocated_usd=allocated,
        unallocated_usd=unallocated,
        unallocated_reasons=unallocated_reasons,
        lines=lines,
        portfolio_value=round(cset.portfolio_value, 2),
        deployment_basis=round(basis, 2),
        base_currency=cset.base_currency,
        rating_as_of=cset.rating_as_of,
        rating_basis=cset.rating_basis,
        config=config,
        champion=LIVE_CHAMPION,
        skipped=skipped,
    )


_UNALLOCATED_REASON_TEXT: dict[str, str] = {
    "no_rating": "some candidates have no published technical rating",
    "label": "candidates rated Neutral / Sell / Strong Sell are not eligible",
    "unpriced": "some candidates have no cached close or live quote",
    "currency": "some candidates are priced in another currency",
    "sector_unknown": "some candidates have no sector classification, so the sector limit can't be evaluated",
    "max_position_weight": "the position-weight limit was reached",
    "max_sector_weight": "the sector-weight limit was reached",
    "min_line_usd": "the largest line some candidates' limits allow is below the minimum line size",
    "cash_exhausted": "the remainder is smaller than the minimum line size",
}
