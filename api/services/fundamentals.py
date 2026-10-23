"""Fundamentals from the alpha-engine-data **data spine** (metron-ops#22 blocks 3-5).

Reads `market_data/fundamentals/latest.json` (produced by alpha-engine-data's
metron_market_data collector, yfinance-derived → licensed → feed-gated), keyed by
yf_symbol. Metron is a pure S3 consumer: a missing artifact / absent symbol → omitted,
never fabricated. The source is injectable for tests.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date

from api.services import spine_artifacts
from portfolio_analytics.admission import Invalid, admit_schema, read_number

logger = logging.getLogger(__name__)

FUNDAMENTALS_KEY = "market_data/fundamentals/latest.json"
# The producer version this reader is written for — the same pin as
# tests/contracts/metron_fundamentals.schema.json. Any other version is refused at
# runtime (metron-ops-I373); re-pin both together when the producer bumps it.
FUNDAMENTALS_SCHEMA_VERSION = 5


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
    book_value_per_share: float | None  # $ (yfinance bookValue, artifact v5) — raw input behind price_to_book
    price_to_sales: float | None   # yfinance priceToSalesTrailing12Months (artifact v2)
    revenue_per_share: float | None  # $ (yfinance revenuePerShare, artifact v5) — raw input behind price_to_sales
    peg: float | None              # derived: trailing P/E ÷ (earnings growth %)
    ev_ebitda: float | None
    enterprise_value: float | None  # $ (yfinance enterpriseValue, artifact v5) — raw input behind ev_ebitda
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
    # Lineage and admission (metron-ops-I373). `schema_version`/`source` are what the
    # artifact declared. `rejected_reason` is set when the whole artifact was refused
    # (unknown/missing schema_version) — `by_symbol` is then empty, exactly as for an
    # absent artifact, but the reason is explicit. `invalid_fields` names, per symbol,
    # each field that was PRESENT but not a finite number (NaN/Infinity/garbage); those
    # fields read as None, and are distinct from fields the producer simply omitted.
    schema_version: int | None = None
    source: str | None = None
    rejected_reason: str | None = None
    invalid_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    # Fail-soft (None on any read error); shared short-TTL cache — see spine_artifacts.
    return spine_artifacts.read_json(FUNDAMENTALS_KEY, bucket=_bucket())


def _f(d: dict, key: str, invalid: list[str]) -> float | None:
    """A finite float, or None. A present-but-invalid value (NaN, Infinity, a
    non-numeric string) also reads as None, and its key is appended to ``invalid``
    so the snapshot can say so (metron-ops-I373)."""
    reading = read_number(d.get(key))
    if isinstance(reading, Invalid):
        invalid.append(key)
        return None
    return reading


def _parse(yf_symbol: str, d: dict, invalid: list[str]) -> TickerFundamentals:
    trailing_pe = _f(d, "trailingPE", invalid)
    earnings_growth = _f(d, "earningsGrowth", invalid)
    # PEG = P/E ÷ (annual earnings growth in %); only meaningful for positive growth.
    peg = (
        trailing_pe / (earnings_growth * 100.0)
        if trailing_pe is not None and earnings_growth not in (None, 0) and earnings_growth > 0
        else None
    )
    div = _f(d, "dividendYield", invalid)
    return TickerFundamentals(
        yf_symbol=yf_symbol,
        sector=d.get("sector"),
        industry=d.get("industry"),
        market_cap=_f(d, "marketCap", invalid),
        beta=_f(d, "beta", invalid),
        trailing_pe=trailing_pe,
        forward_pe=_f(d, "forwardPE", invalid),
        eps=_f(d, "trailingEps", invalid),
        fwd_eps=_f(d, "forwardEps", invalid),
        price_to_book=_f(d, "priceToBook", invalid),
        book_value_per_share=_f(d, "bookValue", invalid),
        price_to_sales=_f(d, "priceToSalesTrailing12Months", invalid),
        revenue_per_share=_f(d, "revenuePerShare", invalid),
        peg=peg,
        ev_ebitda=_f(d, "enterpriseToEbitda", invalid),
        enterprise_value=_f(d, "enterpriseValue", invalid),
        earnings_growth=earnings_growth,
        revenue_growth=_f(d, "revenueGrowth", invalid),
        debt_to_equity=_f(d, "debtToEquity", invalid),
        current_ratio=_f(d, "currentRatio", invalid),
        quick_ratio=_f(d, "quickRatio", invalid),
        total_debt=_f(d, "totalDebt", invalid),
        total_cash=_f(d, "totalCash", invalid),
        ebitda=_f(d, "ebitda", invalid),
        free_cashflow=_f(d, "freeCashflow", invalid),
        roe=_f(d, "returnOnEquity", invalid),
        roa=_f(d, "returnOnAssets", invalid),
        gross_margins=_f(d, "grossMargins", invalid),
        operating_margins=_f(d, "operatingMargins", invalid),
        dividend_yield=(div / 100.0 if div is not None else None),  # percent → fraction
    )


def load_fundamentals(*, reader=None) -> FundamentalsSnapshot:
    """The latest fundamentals snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read."""
    raw = (reader or _default_reader)()
    rejected = admit_schema(raw, expected=FUNDAMENTALS_SCHEMA_VERSION, artifact=FUNDAMENTALS_KEY)
    if rejected is not None:
        return FundamentalsSnapshot(
            as_of=None,
            by_symbol={},
            schema_version=raw.get("schema_version") if isinstance(raw, dict) else None,
            rejected_reason=rejected,
        )
    art = raw or {}
    by_symbol: dict[str, TickerFundamentals] = {}
    invalid_fields: dict[str, tuple[str, ...]] = {}
    for sym, body in (art.get("fundamentals") or {}).items():
        if isinstance(body, dict):
            invalid: list[str] = []
            by_symbol[sym] = _parse(sym, body, invalid)
            if invalid:
                invalid_fields[sym] = tuple(invalid)
    if invalid_fields:
        logger.warning(
            "%s: %d symbol(s) carried non-finite or non-numeric values, read as unavailable: %s",
            FUNDAMENTALS_KEY, len(invalid_fields), sorted(invalid_fields),
        )
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return FundamentalsSnapshot(
        as_of=as_of,
        by_symbol=by_symbol,
        schema_version=art.get("schema_version"),
        source=art.get("source"),
        invalid_fields=invalid_fields,
    )
