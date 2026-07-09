"""Fundamentals from the alpha-engine-data **data spine** (metron-ops#22 blocks 3-5).

Reads `market_data/fundamentals/latest.json` (produced by alpha-engine-data's
metron_market_data collector, yfinance-derived → licensed → feed-gated), keyed by
yf_symbol. Metron is a pure S3 consumer: a missing artifact / absent symbol → omitted,
never fabricated. The source is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

FUNDAMENTALS_KEY = "market_data/fundamentals/latest.json"


@dataclass
class TickerFundamentals:
    yf_symbol: str
    sector: str | None
    industry: str | None
    market_cap: float | None
    beta: float | None
    # Valuation multiples
    trailing_pe: float | None
    forward_pe: float | None
    eps: float | None               # $ (yfinance trailingEps, artifact v4) — raw input behind trailing_pe
    fwd_eps: float | None           # $ (yfinance forwardEps, artifact v4) — raw input behind forward_pe
    price_to_book: float | None    # yfinance priceToBook (artifact v2)
    price_to_sales: float | None   # yfinance priceToSalesTrailing12Months (artifact v2)
    peg: float | None              # derived: trailing P/E ÷ (earnings growth %)
    ev_ebitda: float | None
    earnings_growth: float | None  # fraction
    revenue_growth: float | None   # fraction
    # Balance-sheet ratios + absolute balances ($, artifact v3)
    debt_to_equity: float | None   # raw artifact value (yfinance: a percentage, e.g. 47.2)
    current_ratio: float | None
    quick_ratio: float | None
    total_debt: float | None       # $ (yfinance totalDebt)
    total_cash: float | None       # $ (yfinance totalCash)
    ebitda: float | None           # $ (yfinance ebitda) — for net-debt/EBITDA leverage
    free_cashflow: float | None    # $ (yfinance freeCashflow)
    roe: float | None              # fraction
    roa: float | None              # fraction
    gross_margins: float | None    # fraction
    operating_margins: float | None  # fraction
    dividend_yield: float | None   # fraction (artifact gives a percent → normalized ÷100)


@dataclass
class FundamentalsSnapshot:
    as_of: date | None
    by_symbol: dict[str, TickerFundamentals]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=FUNDAMENTALS_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "fundamentals unavailable"
        logger.warning("data-spine read failed %s: %s", FUNDAMENTALS_KEY, e)
        return None


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse(yf_symbol: str, d: dict) -> TickerFundamentals:
    trailing_pe = _f(d, "trailingPE")
    earnings_growth = _f(d, "earningsGrowth")
    # PEG = P/E ÷ (annual earnings growth in %); only meaningful for positive growth.
    peg = (
        trailing_pe / (earnings_growth * 100.0)
        if trailing_pe is not None and earnings_growth not in (None, 0) and earnings_growth > 0
        else None
    )
    div = _f(d, "dividendYield")
    return TickerFundamentals(
        yf_symbol=yf_symbol,
        sector=d.get("sector"),
        industry=d.get("industry"),
        market_cap=_f(d, "marketCap"),
        beta=_f(d, "beta"),
        trailing_pe=trailing_pe,
        forward_pe=_f(d, "forwardPE"),
        eps=_f(d, "trailingEps"),
        fwd_eps=_f(d, "forwardEps"),
        price_to_book=_f(d, "priceToBook"),
        price_to_sales=_f(d, "priceToSalesTrailing12Months"),
        peg=peg,
        ev_ebitda=_f(d, "enterpriseToEbitda"),
        earnings_growth=earnings_growth,
        revenue_growth=_f(d, "revenueGrowth"),
        debt_to_equity=_f(d, "debtToEquity"),
        current_ratio=_f(d, "currentRatio"),
        quick_ratio=_f(d, "quickRatio"),
        total_debt=_f(d, "totalDebt"),
        total_cash=_f(d, "totalCash"),
        ebitda=_f(d, "ebitda"),
        free_cashflow=_f(d, "freeCashflow"),
        roe=_f(d, "returnOnEquity"),
        roa=_f(d, "returnOnAssets"),
        gross_margins=_f(d, "grossMargins"),
        operating_margins=_f(d, "operatingMargins"),
        dividend_yield=(div / 100.0 if div is not None else None),  # percent → fraction
    )


def load_fundamentals(*, reader=None) -> FundamentalsSnapshot:
    """The latest fundamentals snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read."""
    art = (reader or _default_reader)() or {}
    by_symbol: dict[str, TickerFundamentals] = {}
    for sym, body in (art.get("fundamentals") or {}).items():
        if isinstance(body, dict):
            by_symbol[sym] = _parse(sym, body)
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return FundamentalsSnapshot(as_of=as_of, by_symbol=by_symbol)
