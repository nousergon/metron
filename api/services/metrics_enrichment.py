"""Shared per-ticker metrics enrichment (Holdings metrics — metron-ops#105/#106).

Fills valuation / fundamentals / balance-sheet / technicals / consensus / sentiment +
the composite attractiveness score onto a list of ``analytics.Holding`` rows, keyed purely
by ticker (yf_symbol) — never by quantity/position. Two consumers share this: the Holdings
endpoint (real positions) and the watchlist endpoint (position-optional tracked tickers,
metron-ops#42), so a watchlist entry gets the identical metric pipeline a real holding does
without ever touching NAV/performance (those read Position rows directly and never call
this module).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from api.services import analyst as analyst_service
from api.services import analytics
from api.services import attractiveness as attractiveness_service
from api.services import fundamentals as fundamentals_service
from api.services import sentiment as sentiment_service
from api.services import (
    tearsheet as tearsheet_service,
)
from api.services import technicals as technicals_service


def enrich_metrics(session: Session, held: list[analytics.Holding]) -> None:
    """Fill each holding's valuation/fundamentals/technicals + consensus/sentiment fields
    from the data-spine fundamentals + technicals + analyst + sentiment artifacts (keyed by
    yf_symbol). Fail-soft: a missing artifact or absent symbol leaves the fields None
    (coverage gap, never fabricated)."""
    yf_map = tearsheet_service._yf_symbol_map(session, [h.ticker for h in held])
    funds = fundamentals_service.load_fundamentals().by_symbol
    techs = technicals_service.load_technicals().by_symbol
    analysts = analyst_service.load_analyst().by_symbol
    sentiments = sentiment_service.load_sentiment().by_symbol
    universe_att = attractiveness_service.compute_universe()
    for h in held:
        yf = yf_map.get(h.ticker, h.ticker)
        f = funds.get(yf)
        if f is not None:
            h.market_cap = f.market_cap
            h.pe = f.trailing_pe
            h.fwd_pe = f.forward_pe
            h.eps = f.eps
            h.fwd_eps = f.fwd_eps
            h.pb = f.price_to_book
            h.ps = f.price_to_sales
            h.ev_ebitda = f.ev_ebitda
            h.ebitda = f.ebitda
            h.peg = f.peg
            h.div_yield = f.dividend_yield
            h.rev_growth = f.revenue_growth
            h.earnings_growth = f.earnings_growth
            h.gross_margin = f.gross_margins
            h.op_margin = f.operating_margins
            h.roe = f.roe
            h.roa = f.roa
            h.beta = f.beta
            # Balance sheet: absolute balances + derived net debt / leverage.
            h.cash = f.total_cash
            h.debt = f.total_debt
            h.debt_to_equity = f.debt_to_equity
            h.current_ratio = f.current_ratio
            h.quick_ratio = f.quick_ratio
            h.fcf = f.free_cashflow
            if f.total_debt is not None and f.total_cash is not None:
                h.net_debt = f.total_debt - f.total_cash
                if f.ebitda not in (None, 0):
                    h.net_debt_to_ebitda = h.net_debt / f.ebitda
        t = techs.get(yf)
        if t is not None:
            h.rsi_14 = t.rsi_14
            h.macd_hist = t.macd_hist
            h.pct_to_ma_50 = t.pct_to_ma_50
            h.pct_to_ma_200 = t.pct_to_ma_200
            h.pct_in_52w_range = t.pct_in_52w_range
            h.mom_20d = t.mom_20d
        # Consensus research (metron-ops#105) — price-target upside derived vs the live price.
        a = analysts.get(yf)
        if a is not None:
            h.consensus_rating = a.consensus_rating
            h.consensus_score = a.rating_score
            h.price_target_mean = a.mean_target
            h.price_target_median = a.median_target
            h.num_analysts = a.num_analysts
            h.price_target_upside = a.target_upside(h.last_price)
        # News sentiment (metron-ops#105).
        s = sentiments.get(yf)
        if s is not None:
            h.news_sentiment = s.sentiment
            h.news_articles = s.n_articles
        att = attractiveness_service.lookup(yf, universe_att)
        if att is not None:
            h.attractiveness = att.score
            h.attractiveness_coverage = att.coverage
            by_key = {p.key: p.score for p in att.pillars}
            h.attractiveness_quality = by_key.get("quality")
            h.attractiveness_value = by_key.get("value")
            h.attractiveness_momentum = by_key.get("momentum")
            h.attractiveness_growth = by_key.get("growth")
            h.attractiveness_stewardship = by_key.get("stewardship")
            h.attractiveness_defensiveness = by_key.get("defensiveness")
