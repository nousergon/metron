"""Per-holding tearsheet (metron-ops#22) — an IB-style page for one ticker.

Six blocks. What's computable from data Metron already has is computed now; the
fundamentals-dependent blocks (multiples, balance-sheet ratios, comps) are honestly
marked unavailable until the fundamentals spine artifact ships (alpha-engine-config#1022)
rather than fabricated.

- Position: shares, cost, market value, weight, accounts held in (always — broker data).
- Performance: return vs cost (always); period returns + Sharpe/Sortino/max-drawdown/vol
  + beta-vs-SPY computed from the cached PriceBar history when enough bars exist, else None.
- Technical: RSI(14) + % from 52-week high from price bars; forward div yield is N/A
  (fundamentals).
- Valuation multiples / balance sheet / comps: N/A until #1022.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from nousergon_lib.quant.riskstats import max_drawdown, sharpe_ratio, sortino_ratio, volatility
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analyst as analyst_service
from api.services import analytics
from api.services import fundamentals as fundamentals_service
from api.services import sentiment as sentiment_service

_SPY = "SPY"
_MIN_RISK_BARS = 60          # ~3 months of daily closes before annualized risk stats mean anything
_RSI_PERIOD = 14
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
    return_vs_cost: float | None     # = unrealized_pct (the only return that needs no price history)
    period_returns: dict[str, float] = field(default_factory=dict)  # "1Y"/"3Y"/"5Y"/"10Y" → return
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    beta_vs_spy: float | None = None
    vs_spy: float | None = None      # ticker total return − SPY total return over the cached window
    n_bars: int = 0
    history_from: date | None = None


@dataclass
class TearsheetTechnical:
    rsi_14: float | None = None
    pct_from_52wk_high: float | None = None
    forward_div_yield: float | None = None   # N/A until fundamentals (#1022)


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
    # Paid forward-estimate scaffolding (metron-ops#107): present columns, resolve N/A until
    # the paid consensus-estimates feed lands — no schema/UI change then.
    estimates_available: bool = False
    estimates_reason: str = analyst_service.PAID_ESTIMATES_REASON
    forward_eps: float | None = None
    forward_revenue: float | None = None
    forward_pe_consensus: float | None = None
    peg_consensus: float | None = None
    estimate_revision_trend: float | None = None


@dataclass
class Tearsheet:
    ticker: str
    base_currency: str
    as_of: date
    position: TearsheetPosition
    performance: TearsheetPerformance
    technical: TearsheetTechnical
    # Multiples / balance-sheet / comps blocks come from the fundamentals spine artifact
    # (alpha-engine-config#1022), which is feed-gated (yfinance-derived → Pro). Populated on
    # a feed-entitled build; honestly N/A otherwise.
    fundamentals_available: bool = False
    fundamentals_reason: str = _FUNDAMENTALS_REASON
    fundamentals: fundamentals_service.TickerFundamentals | None = None
    fundamentals_as_of: date | None = None
    comps: list[Comp] = field(default_factory=list)
    # Consensus research + news sentiment (metron-ops#105) — feed-gated, free-source spine.
    consensus_available: bool = False
    consensus_as_of: date | None = None
    consensus: TearsheetConsensus = field(default_factory=TearsheetConsensus)


def _close_series(session: Session, symbol: str) -> list[tuple[date, float]]:
    """Ascending (bar_date, close) series for a symbol from the global PriceBar cache."""
    rows = session.execute(
        select(models.PriceBar.bar_date, models.PriceBar.close)
        .join(models.Security, models.PriceBar.security_id == models.Security.id)
        .where(models.Security.symbol == symbol)
        .order_by(models.PriceBar.bar_date)
    ).all()
    # One Security per symbol drives pricing; if duplicates exist, de-dupe by date (last wins).
    by_date: dict[date, float] = {}
    for bar_date, close in rows:
        if close is not None:
            by_date[bar_date] = float(close)
    return sorted(by_date.items())


def _daily_returns(closes: list[float]) -> list[float]:
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            out.append(closes[i] / closes[i - 1] - 1.0)
    return out


def _period_returns(series: list[tuple[date, float]], as_of: date) -> dict[str, float]:
    """Total return over trailing 1/3/5/10 calendar years — the first bar on/after the
    window start vs the latest bar. A window with no bar reaching that far back is omitted."""
    if len(series) < 2:
        return {}
    last_close = series[-1][1]
    out: dict[str, float] = {}
    for years, label in ((1, "1Y"), (3, "3Y"), (5, "5Y"), (10, "10Y")):
        try:
            start = as_of.replace(year=as_of.year - years)
        except ValueError:  # Feb-29 → Feb-28
            start = as_of.replace(year=as_of.year - years, day=28)
        ref = next((c for d, c in series if d >= start), None)
        if ref is not None and ref > 0 and series[0][0] <= start:
            out[label] = last_close / ref - 1.0
    return out


def _beta_and_alpha(
    ticker_series: list[tuple[date, float]], spy_series: list[tuple[date, float]]
) -> tuple[float | None, float | None]:
    """Beta vs SPY and total-return spread over the common-date window. Beta = cov/var of
    the aligned daily returns; both None without enough overlap."""
    spy_by_date = dict(spy_series)
    common = [(d, c, spy_by_date[d]) for d, c in ticker_series if d in spy_by_date]
    if len(common) < _MIN_RISK_BARS:
        return None, None
    t_closes = [c for _, c, _ in common]
    s_closes = [s for _, _, s in common]
    t_rets, s_rets = _daily_returns(t_closes), _daily_returns(s_closes)
    n = min(len(t_rets), len(s_rets))
    if n < _MIN_RISK_BARS or not s_rets:
        return None, None
    t_rets, s_rets = t_rets[:n], s_rets[:n]
    s_mean = sum(s_rets) / n
    t_mean = sum(t_rets) / n
    var = sum((s - s_mean) ** 2 for s in s_rets) / n
    if var <= 0:
        return None, None
    cov = sum((t_rets[i] - t_mean) * (s_rets[i] - s_mean) for i in range(n)) / n
    beta = cov / var
    vs_spy = (t_closes[-1] / t_closes[0] - 1.0) - (s_closes[-1] / s_closes[0] - 1.0)
    return beta, vs_spy


def _rsi_14(closes: list[float]) -> float | None:
    """Wilder's RSI(14). None without enough history."""
    if len(closes) <= _RSI_PERIOD:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:_RSI_PERIOD]) / _RSI_PERIOD
    avg_loss = sum(losses[:_RSI_PERIOD]) / _RSI_PERIOD
    for i in range(_RSI_PERIOD, len(gains)):
        avg_gain = (avg_gain * (_RSI_PERIOD - 1) + gains[i]) / _RSI_PERIOD
        avg_loss = (avg_loss * (_RSI_PERIOD - 1) + losses[i]) / _RSI_PERIOD
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _performance(session: Session, ticker: str, unrealized_pct: float | None, as_of: date) -> TearsheetPerformance:
    perf = TearsheetPerformance(return_vs_cost=unrealized_pct)
    series = _close_series(session, ticker)
    perf.n_bars = len(series)
    if not series:
        return perf
    perf.history_from = series[0][0]
    perf.period_returns = _period_returns(series, as_of)
    closes = [c for _, c in series]
    rets = _daily_returns(closes)
    if len(rets) >= _MIN_RISK_BARS:
        span_days = (series[-1][0] - series[0][0]).days or 1
        ppy = len(rets) / (span_days / 365.25)
        perf.volatility = volatility(rets, periods_per_year=ppy)
        perf.sharpe = sharpe_ratio(rets, periods_per_year=ppy)
        perf.sortino = sortino_ratio(rets, periods_per_year=ppy)
        index = [1.0]
        for r in rets:
            index.append(index[-1] * (1.0 + r))
        perf.max_drawdown = max_drawdown(index)
        perf.beta_vs_spy, perf.vs_spy = _beta_and_alpha(series, _close_series(session, _SPY))
    return perf


def _technical(session: Session, ticker: str) -> TearsheetTechnical:
    series = _close_series(session, ticker)
    closes = [c for _, c in series]
    tech = TearsheetTechnical()
    tech.rsi_14 = _rsi_14(closes)
    if closes:
        window = closes[-252:]  # ~1 trading year
        high = max(window)
        if high > 0:
            tech.pct_from_52wk_high = closes[-1] / high - 1.0
    return tech


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
) -> Tearsheet | None:
    """Assemble the tearsheet for one held ticker, or None if the portfolio doesn't hold it.

    ``feed_enabled`` gates the fundamentals blocks (multiples / balance-sheet / comps) —
    they're yfinance-derived (licensed) so they only populate on a feed-entitled build;
    otherwise they're honestly N/A. ``fundamentals_reader`` is injectable for tests."""
    base = analytics._base_currency(session, portfolio_id)
    valued = analytics.valued_holdings(session, tenant_id, portfolio_id)
    holding = next((h for h in valued if h.ticker == ticker), None)
    if holding is None:
        return None

    # Portfolio weight = this ticker's market value over the total priced market value.
    total_mv = sum(h.market_value for h in valued if h.market_value is not None)
    weight = holding.market_value / total_mv if (holding.market_value is not None and total_mv) else None

    # Accounts the ticker is held in (by nickname/name).
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
    as_of = date.today()
    technical = _technical(session, ticker)
    sheet = Tearsheet(
        ticker=ticker,
        base_currency=base,
        as_of=as_of,
        position=position,
        performance=_performance(session, ticker, holding.unrealized_pct, as_of),
        technical=technical,
    )

    # Fundamentals blocks (multiples / balance-sheet / comps) — feed-gated.
    if feed_enabled:
        snap = fundamentals_service.load_fundamentals(reader=fundamentals_reader)
        yf_map = _yf_symbol_map(session, [h.ticker for h in valued])
        fund = snap.by_symbol.get(yf_map.get(ticker, ticker))
        if fund is not None:
            sheet.fundamentals_available = True
            sheet.fundamentals = fund
            sheet.fundamentals_as_of = snap.as_of
            technical.forward_div_yield = fund.dividend_yield
            # Same-sector comps across the user's holdings (the target row flagged is_self).
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

        # Consensus research + news sentiment panel (metron-ops#105) — feed-gated, fail-soft:
        # a missing artifact / absent symbol leaves the panel empty (never breaks the page).
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
                # Paid forward-estimate scaffolding (metron-ops#107) — N/A until the feed lands.
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
    return sheet
