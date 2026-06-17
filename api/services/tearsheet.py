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

from alpha_engine_lib.quant.riskstats import max_drawdown, sharpe_ratio, sortino_ratio, volatility
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics

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
class Tearsheet:
    ticker: str
    base_currency: str
    as_of: date
    position: TearsheetPosition
    performance: TearsheetPerformance
    technical: TearsheetTechnical
    # Multiples / balance-sheet / comps blocks are gated on the fundamentals artifact.
    fundamentals_available: bool = False
    fundamentals_reason: str = _FUNDAMENTALS_REASON


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


def tearsheet(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, ticker: str) -> Tearsheet | None:
    """Assemble the tearsheet for one held ticker, or None if the portfolio doesn't hold it."""
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
    return Tearsheet(
        ticker=ticker,
        base_currency=base,
        as_of=as_of,
        position=position,
        performance=_performance(session, ticker, holding.unrealized_pct, as_of),
        technical=_technical(session, ticker),
    )
