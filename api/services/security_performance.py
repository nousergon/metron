"""Security performance metrics from the alpha-engine-data **data spine**.

Reads ``market_data/security_performance/latest.json`` (produced daily by
alpha-engine-data's metron_market_data collector from close_history + SPY — yfinance-derived
→ feed-gated). Canonical source for Metron tearsheet period returns / risk stats and Holdings
YTD/LTM — consumers never recompute from local price bars.

Mirrors ``technicals.py`` / ``fundamentals.py`` (injectable reader, fail-soft).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

SECURITY_PERFORMANCE_KEY = "market_data/security_performance/latest.json"


@dataclass
class TickerPerformance:
    yf_symbol: str
    period_returns: dict[str, float] = field(default_factory=dict)  # 1Y/3Y/5Y/10Y
    ytd_pct: float | None = None
    ltm_pct: float | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    beta_vs_spy: float | None = None
    vs_spy_1y: float | None = None       # 1Y ticker return − 1Y SPY return
    vs_spy_window: float | None = None   # overlap-window total-return spread
    n_bars: int = 0
    history_from: date | None = None


@dataclass
class SecurityPerformanceSnapshot:
    as_of: date | None
    by_symbol: dict[str, TickerPerformance]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=SECURITY_PERFORMANCE_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:
        logger.warning("data-spine read failed %s: %s", SECURITY_PERFORMANCE_KEY, e)
        return None


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse(yf_symbol: str, d: dict) -> TickerPerformance:
    period_returns: dict[str, float] = {}
    raw_pr = d.get("period_returns")
    if isinstance(raw_pr, dict):
        for label, val in raw_pr.items():
            try:
                period_returns[str(label)] = float(val)
            except (TypeError, ValueError):
                continue
    history_from = None
    raw_hf = d.get("history_from")
    if raw_hf:
        try:
            history_from = date.fromisoformat(str(raw_hf)[:10])
        except ValueError:
            history_from = None
    n_bars = 0
    try:
        n_bars = int(d.get("n_bars") or 0)
    except (TypeError, ValueError):
        n_bars = 0
    return TickerPerformance(
        yf_symbol=yf_symbol,
        period_returns=period_returns,
        ytd_pct=_f(d, "ytd_pct"),
        ltm_pct=_f(d, "ltm_pct"),
        volatility=_f(d, "volatility"),
        sharpe=_f(d, "sharpe"),
        sortino=_f(d, "sortino"),
        max_drawdown=_f(d, "max_drawdown"),
        beta_vs_spy=_f(d, "beta_vs_spy"),
        vs_spy_1y=_f(d, "vs_spy_1y"),
        vs_spy_window=_f(d, "vs_spy_window"),
        n_bars=n_bars,
        history_from=history_from,
    )


def load_security_performance(*, reader=None) -> SecurityPerformanceSnapshot:
    """Latest performance snapshot keyed by yf_symbol. ``reader`` is injectable for tests."""
    art = (reader or _default_reader)() or {}
    by_symbol: dict[str, TickerPerformance] = {}
    for sym, body in (art.get("performance") or {}).items():
        if isinstance(body, dict):
            by_symbol[sym] = _parse(sym, body)
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return SecurityPerformanceSnapshot(as_of=as_of, by_symbol=by_symbol)
