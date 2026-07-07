"""Technical indicators from the alpha-engine-data **data spine** (Holdings metrics).

Reads `market_data/technicals/latest.json` (produced daily by alpha-engine-data's
metron_market_data collector from the close_history it already publishes — yfinance-derived
→ feed-gated), keyed by yf_symbol. Metron is a pure S3 consumer: a missing artifact / absent
symbol → omitted, never fabricated. The source is injectable for tests.

Mirrors `fundamentals.py` exactly (module-level KEY, fail-soft `_default_reader`, injectable
`reader`, dataclass + `load_*`).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

TECHNICALS_KEY = "market_data/technicals/latest.json"


@dataclass
class TickerTechnicals:
    yf_symbol: str
    rsi_14: float | None          # Wilder RSI(14), 0-100
    macd_hist: float | None       # MACD line − signal line (price units)
    ma_50: float | None
    ma_200: float | None
    pct_to_ma_50: float | None    # last/50d-MA − 1 (fraction)
    pct_to_ma_200: float | None   # last/200d-MA − 1 (fraction)
    high_52w: float | None
    low_52w: float | None
    pct_in_52w_range: float | None  # (last − low)/(high − low), 0-1
    pct_from_52wk_high: float | None  # last/high_52w − 1 (fraction)
    mom_20d: float | None         # 20-session return (fraction)
    mom_60d: float | None         # 60-session return (fraction)


@dataclass
class TechnicalsSnapshot:
    as_of: date | None
    by_symbol: dict[str, TickerTechnicals]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=TECHNICALS_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "technicals unavailable"
        logger.warning("data-spine read failed %s: %s", TECHNICALS_KEY, e)
        return None


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse(yf_symbol: str, d: dict) -> TickerTechnicals:
    return TickerTechnicals(
        yf_symbol=yf_symbol,
        rsi_14=_f(d, "rsi_14"),
        macd_hist=_f(d, "macd_hist"),
        ma_50=_f(d, "ma_50"),
        ma_200=_f(d, "ma_200"),
        pct_to_ma_50=_f(d, "pct_to_ma_50"),
        pct_to_ma_200=_f(d, "pct_to_ma_200"),
        high_52w=_f(d, "high_52w"),
        low_52w=_f(d, "low_52w"),
        pct_in_52w_range=_f(d, "pct_in_52w_range"),
        pct_from_52wk_high=_f(d, "pct_from_52wk_high"),
        mom_20d=_f(d, "mom_20d"),
        mom_60d=_f(d, "mom_60d"),
    )


def load_technicals(*, reader=None) -> TechnicalsSnapshot:
    """The latest technicals snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read."""
    art = (reader or _default_reader)() or {}
    by_symbol: dict[str, TickerTechnicals] = {}
    for sym, body in (art.get("technicals") or {}).items():
        if isinstance(body, dict):
            by_symbol[sym] = _parse(sym, body)
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return TechnicalsSnapshot(as_of=as_of, by_symbol=by_symbol)
