"""Per-holding tearsheet (metron-ops#22) — an IB-style page for one ticker.

Six blocks. Position economics come from broker data (always). Performance period returns,
risk stats, beta, and technical indicators are **pure consumers** of the alpha-engine-data
spine artifacts (``security_performance/latest.json``, ``technicals/latest.json``) —
feed-gated, never recomputed from local ``price_bars``. Fundamentals-dependent blocks
(multiples, balance-sheet, comps) consume ``fundamentals/latest.json`` the same way.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analyst as analyst_service
from api.services import analytics
from api.services import attractiveness as attractiveness_service
from api.services import fundamentals as fundamentals_service
from api.services import security_perf as security_perf_service
from api.services import security_performance as performance_service
from api.services import sentiment as sentiment_service
from api.services import technicals as technicals_service

_FUNDAMENTALS_REASON = "Arrives with the fundamentals feed (alpha-engine-config#1022)."


@dataclass
class TearsheetPosition:
    ticker: str
    currency: str
    quantity: float
    avg_cost: float
    cost_basis: float | None        # base currency
    market_value: float | None      # base currency
    unrealized_gain: float | None
    unrealized_pct: float | None
    weight_pct: float | None        # share of the portfolio's priced market value
    accounts: list[str] = field(default_factory=list)


@dataclass
class TearsheetPerformance:
    return_vs_cost: float | None     # = unrealized_pct (broker — no price history needed)
    period_returns: dict[str, float] = field(default_factory=dict)  # "1Y"/"3Y"/"5Y"/"10Y" → return
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    beta_vs_spy: float | None = None
    vs_spy: float | None = None          # overlap-window total return − SPY (spine ``vs_spy_window``)
    vs_spy_1y: float | None = None       # 1Y ticker return − 1Y SPY return
    n_bars: int = 0
    history_from: date | None = None


@dataclass
class TearsheetTechnical:
    rsi_14: float | None = None
    pct_from_52wk_high: float | None = None
    forward_div_yield: float | None = None   # from fundamentals when feed-enabled


@dataclass
class Comp:
    """One row of the same-sector comparison table (the holding + its sector peers)."""

    ticker: str
    sector: str | None
    trailing_pe: float | None
    forward_pe: float | None
    ev_ebitda: float | None
    debt_to_equity: float | None
    dividend_yield: float | None
    is_self: bool = False


@dataclass
class TearsheetConsensus:
    """Consensus research + news sentiment panel (metron-ops#105). Free-source data spine,
    feed-gated; honestly empty (all None) off a feed-entitled build or on a coverage gap.
    The paid forward-estimate columns are scaffolded N/A until metron-ops#107."""

    consensus_rating: str | None = None
    consensus_score: float | None = None        # signed [-1, +1]
    price_target_mean: float | None = None
    price_target_median: float | None = None
    price_target_upside: float | None = None     # mean target / market price − 1 (fraction)
    num_analysts: int | None = None
    news_sentiment: float | None = None          # trust-weighted LM composite ∈ [-1, +1]
    news_articles: int | None = None
    news_as_of: date | None = None               # the sentiment slice's own freshness anchor
    estimates_available: bool = False
    estimates_reason: str = analyst_service.PAID_ESTIMATES_REASON
    forward_eps: float | None = None
    forward_revenue: float | None = None
    forward_pe_consensus: float | None = None
    peg_consensus: float | None = None
    estimate_revision_trend: float | None = None


@dataclass
class TearsheetAttractivenessComponent:
    key: str
    weight: float
    score: float
    contribution: float | None = None


@dataclass
class TearsheetAttractiveness:
    """SOTA 6-pillar attractiveness gauge: cross-sectional percentile + pillar breakdown."""

    available: bool = False
    score: float | None = None
    coverage: int | None = None
    components: list[TearsheetAttractivenessComponent] = field(default_factory=list)


@dataclass
class Tearsheet:
    ticker: str
    base_currency: str
    as_of: date
    position: TearsheetPosition
    performance: TearsheetPerformance
    technical: TearsheetTechnical
    fundamentals_available: bool = False
    fundamentals_reason: str = _FUNDAMENTALS_REASON
    fundamentals: fundamentals_service.TickerFundamentals | None = None
    fundamentals_as_of: date | None = None
    comps: list[Comp] = field(default_factory=list)
    consensus_available: bool = False
    consensus_as_of: date | None = None
    consensus: TearsheetConsensus = field(default_factory=TearsheetConsensus)
    attractiveness: TearsheetAttractiveness = field(default_factory=TearsheetAttractiveness)


def _performance_from_spine(
    row: performance_service.TickerPerformance | None, unrealized_pct: float | None
) -> TearsheetPerformance:
    perf = TearsheetPerformance(return_vs_cost=unrealized_pct)
    if row is None:
        return perf
    perf.period_returns = dict(row.period_returns)
    perf.volatility = row.volatility
    perf.sharpe = row.sharpe
    perf.sortino = row.sortino
    perf.max_drawdown = row.max_drawdown
    perf.beta_vs_spy = row.beta_vs_spy
    perf.vs_spy = row.vs_spy_window
    perf.vs_spy_1y = row.vs_spy_1y
    perf.n_bars = row.n_bars
    perf.history_from = row.history_from
    return perf


def _technical_from_spine(row: technicals_service.TickerTechnicals | None) -> TearsheetTechnical:
    if row is None:
        return TearsheetTechnical()
    return TearsheetTechnical(
        rsi_14=row.rsi_14,
        pct_from_52wk_high=row.pct_from_52wk_high,
    )


def _yf_symbol_map(session: Session, symbols: list[str]) -> dict[str, str]:
    """ticker → yf_symbol (the fundamentals/intraday artifacts are keyed by yf_symbol).
    Falls back to the bare symbol when no override is set (the US/USD case)."""
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.Security.yf_symbol).where(models.Security.symbol.in_(symbols))
    ).all()
    return {sym: (yf or sym) for sym, yf in rows}


def tearsheet(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    ticker: str,
    *,
    feed_enabled: bool = False,
    fundamentals_reader=None,
    analyst_reader=None,
    sentiment_reader=None,
    performance_reader=None,
    technicals_reader=None,
) -> Tearsheet | None:
    """Assemble the tearsheet for one held ticker, or None if the portfolio doesn't hold it.

    ``feed_enabled`` gates spine-derived market metrics (performance, technicals, fundamentals,
    consensus). Off-feed builds show broker position economics only — never locally recomputed
    period returns or RSI."""
    base = analytics._base_currency(session, portfolio_id)
    valued = analytics.valued_holdings(session, tenant_id, portfolio_id)
    holding = next((h for h in valued if h.ticker == ticker), None)
    if holding is None:
        return None

    total_mv = sum(h.market_value for h in valued if h.market_value is not None)
    weight = holding.market_value / total_mv if (holding.market_value is not None and total_mv) else None

    by_account = analytics.valued_holdings_by_account(session, tenant_id, portfolio_id)
    acct_rows = {a.account_id: a for a in analytics.accounts(session, tenant_id, portfolio_id)}
    account_names = [
        (acct_rows[aid].nickname or acct_rows[aid].name or acct_rows[aid].external_id)
        for aid, hs in by_account.items()
        if aid in acct_rows and any(h.ticker == ticker for h in hs)
    ]

    position = TearsheetPosition(
        ticker=ticker,
        currency=holding.currency,
        quantity=holding.quantity,
        avg_cost=holding.avg_cost,
        cost_basis=holding.cost_basis_base,
        market_value=holding.market_value,
        unrealized_gain=holding.unrealized_gain,
        unrealized_pct=holding.unrealized_pct,
        weight_pct=weight,
        accounts=sorted(account_names),
    )
    as_of = security_perf_service.market_today()

    if feed_enabled:
        yf = _yf_symbol_map(session, [ticker]).get(ticker, ticker)
        perf_snap = performance_service.load_security_performance(reader=performance_reader)
        tech_snap = technicals_service.load_technicals(reader=technicals_reader)
        performance = _performance_from_spine(perf_snap.by_symbol.get(yf), holding.unrealized_pct)
        technical = _technical_from_spine(tech_snap.by_symbol.get(yf))
    else:
        performance = TearsheetPerformance(return_vs_cost=holding.unrealized_pct)
        technical = TearsheetTechnical()

    sheet = Tearsheet(
        ticker=ticker,
        base_currency=base,
        as_of=as_of,
        position=position,
        performance=performance,
        technical=technical,
    )

    if feed_enabled:
        snap = fundamentals_service.load_fundamentals(reader=fundamentals_reader)
        yf_map = _yf_symbol_map(session, [h.ticker for h in valued])
        fund = snap.by_symbol.get(yf_map.get(ticker, ticker))
        if fund is not None:
            sheet.fundamentals_available = True
            sheet.fundamentals = fund
            sheet.fundamentals_as_of = snap.as_of
            technical.forward_div_yield = fund.dividend_yield
            if fund.sector:
                comps: list[Comp] = []
                for h in valued:
                    f = snap.by_symbol.get(yf_map.get(h.ticker, h.ticker))
                    if f is not None and f.sector == fund.sector:
                        comps.append(
                            Comp(
                                ticker=h.ticker,
                                sector=f.sector,
                                trailing_pe=f.trailing_pe,
                                forward_pe=f.forward_pe,
                                ev_ebitda=f.ev_ebitda,
                                debt_to_equity=f.debt_to_equity,
                                dividend_yield=f.dividend_yield,
                                is_self=(h.ticker == ticker),
                            )
                        )
                sheet.comps = sorted(comps, key=lambda c: (not c.is_self, c.ticker))

        yf = yf_map.get(ticker, ticker)
        a_snap = analyst_service.load_analyst(reader=analyst_reader)
        a = a_snap.by_symbol.get(yf)
        s_snap = sentiment_service.load_sentiment(reader=sentiment_reader)
        s = s_snap.by_symbol.get(yf)
        if a is not None or s is not None:
            sheet.consensus_available = True
            sheet.consensus_as_of = a_snap.as_of or s_snap.as_of
            con = sheet.consensus
            if a is not None:
                con.consensus_rating = a.consensus_rating
                con.consensus_score = a.rating_score
                con.price_target_mean = a.mean_target
                con.price_target_median = a.median_target
                con.num_analysts = a.num_analysts
                con.price_target_upside = a.target_upside(holding.last_price)
                con.estimates_available = a.estimates_available
                con.forward_eps = a.forward_eps
                con.forward_revenue = a.forward_revenue
                con.forward_pe_consensus = a.forward_pe_consensus
                con.peg_consensus = a.peg_consensus
                con.estimate_revision_trend = a.estimate_revision_trend
            if s is not None:
                con.news_sentiment = s.sentiment
                con.news_articles = s.n_articles
                con.news_as_of = s.as_of

        yf = _yf_symbol_map(session, [ticker.upper()]).get(ticker.upper(), ticker.upper())
        att = attractiveness_service.lookup(yf, attractiveness_service.compute_universe())
        if att is not None and att.score is not None:
            sheet.attractiveness = TearsheetAttractiveness(
                available=True,
                score=att.score,
                coverage=att.coverage,
                components=[
                    TearsheetAttractivenessComponent(
                        key=p.key,
                        weight=p.weight,
                        score=p.score,
                        contribution=p.contribution,
                    )
                    for p in att.pillars
                ],
            )
    return sheet
