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
from api.services import valuation_medians as valuation_medians_service


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
    # Sector/country median multiples — the peer benchmark for the attractiveness valuation
    # component (metron-ops#106). Fail-soft: a missing artifact leaves medians empty → the
    # valuation component is simply dropped from the renormalized blend.
    medians = valuation_medians_service.load_valuation_medians()
    for h in held:
        yf = yf_map.get(h.ticker, h.ticker)
        f = funds.get(yf)
        if f is not None:
            h.market_cap = f.market_cap
            h.pe = f.trailing_pe
            h.fwd_pe = f.forward_pe
            h.pb = f.price_to_book
            h.ps = f.price_to_sales
            h.ev_ebitda = f.ev_ebitda
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
        # Composite attractiveness score (metron-ops#106, Phase 2) — a transparent blend of the
        # fields just set. The valuation leg bands fwd-P/E against the holding's sector median
        # (country median as a fallback), exactly as the Holdings "by sector → country" view
        # does. Components with no input drop out and the weights renormalize (never fabricated).
        sec_grp = medians.by_sector.get(h.sector) if h.sector else None
        cty_grp = medians.by_country.get(h.country) if h.country else None
        median_fwd_pe = (sec_grp.forward_pe if sec_grp else None)
        if median_fwd_pe is None and cty_grp is not None:
            median_fwd_pe = cty_grp.forward_pe
        att = attractiveness_service.compute(
            fwd_pe=h.fwd_pe,
            median_fwd_pe=median_fwd_pe,
            price_target_upside=h.price_target_upside,
            consensus_score=h.consensus_score,
            estimate_revision_trend=(a.estimate_revision_trend if a is not None else None),
            news_sentiment=h.news_sentiment,
        )
        if att is not None:
            h.attractiveness = att.score
            h.attractiveness_coverage = att.coverage
            # Surface the same component sub-scores the tearsheet gauge shows onto the
            # Holdings/watchlist row too, so the "Attractiveness" band doesn't require a
            # tearsheet click. A component absent from `att.components` (input missing,
            # dropped from the renormalized blend) leaves its field None — never fabricated.
            by_key = {c.key: c.sub_score for c in att.components}
            h.attractiveness_valuation = by_key.get("valuation")
            h.attractiveness_upside = by_key.get("upside")
            h.attractiveness_rating = by_key.get("rating")
            h.attractiveness_revision = by_key.get("revision")
            h.attractiveness_sentiment = by_key.get("sentiment")
