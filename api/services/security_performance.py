"""Security performance metrics from the alpha-engine-data **data spine**.

Reads ``market_data/security_performance/latest.json`` (produced daily by
alpha-engine-data's metron_market_data collector from close_history + SPY — yfinance-derived
→ feed-gated). Canonical source for Metron tearsheet period returns / risk stats and Holdings
YTD/LTM — consumers never recompute from local price bars.

Mirrors ``technicals.py`` / ``fundamentals.py`` (injectable reader, fail-soft).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date

from api.services import spine_artifacts
from portfolio_analytics.admission import admit_schema, finite_field, read_number

logger = logging.getLogger(__name__)

SECURITY_PERFORMANCE_KEY = "market_data/security_performance/latest.json"
# Pinned producer version (tests/contracts/metron_security_performance.schema.json). Any
# other version is refused whole at runtime (metron-ops-I373).
SECURITY_PERFORMANCE_SCHEMA_VERSION = 1


@dataclass
class TickerPerformance:
    yf_symbol: str
    period_returns: dict[str, float] = field(default_factory=dict)  # 1Y/3Y/5Y/10Y
    mtd_pct: float | None = None  # from the prior month's last close
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
    # Set when the artifact was refused whole (off-contract schema_version): it then reads
    # as absent, but the reason is explicit. `invalid_fields` names, per symbol, the fields
    # that were present but not a finite number and therefore read as unavailable.
    rejected_reason: str | None = None
    invalid_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    # Fail-soft (None on any read error); shared short-TTL cache — see spine_artifacts.
    return spine_artifacts.read_json(SECURITY_PERFORMANCE_KEY, bucket=_bucket())


def _parse(yf_symbol: str, d: dict, invalid: list[str]) -> TickerPerformance:
    def _f(key: str) -> float | None:
        return finite_field(d, key, invalid)

    period_returns: dict[str, float] = {}
    raw_pr = d.get("period_returns")
    if isinstance(raw_pr, dict):
        for label, val in raw_pr.items():
            if val is None:
                continue
            reading = read_number(val)
            if isinstance(reading, float):
                period_returns[str(label)] = reading
            else:
                invalid.append(f"period_returns.{label}")
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
        mtd_pct=_f("mtd_pct"),
        ytd_pct=_f("ytd_pct"),
        ltm_pct=_f("ltm_pct"),
        volatility=_f("volatility"),
        sharpe=_f("sharpe"),
        sortino=_f("sortino"),
        max_drawdown=_f("max_drawdown"),
        beta_vs_spy=_f("beta_vs_spy"),
        vs_spy_1y=_f("vs_spy_1y"),
        vs_spy_window=_f("vs_spy_window"),
        n_bars=n_bars,
        history_from=history_from,
    )


def load_security_performance(*, reader=None) -> SecurityPerformanceSnapshot:
    """Latest performance snapshot keyed by yf_symbol. ``reader`` is injectable for tests."""
    raw = (reader or _default_reader)()
    rejected = admit_schema(
        raw, expected=SECURITY_PERFORMANCE_SCHEMA_VERSION, artifact=SECURITY_PERFORMANCE_KEY
    )
    if rejected is not None:
        return SecurityPerformanceSnapshot(as_of=None, by_symbol={}, rejected_reason=rejected)
    art = raw or {}
    by_symbol: dict[str, TickerPerformance] = {}
    invalid_fields: dict[str, tuple[str, ...]] = {}
    for sym, body in (art.get("performance") or {}).items():
        if isinstance(body, dict):
            invalid: list[str] = []
            by_symbol[sym] = _parse(sym, body, invalid)
            if invalid:
                invalid_fields[sym] = tuple(invalid)
    if invalid_fields:
        logger.warning(
            "%s: %d symbol(s) carried non-finite or non-numeric values, read as unavailable: %s",
            SECURITY_PERFORMANCE_KEY, len(invalid_fields), sorted(invalid_fields),
        )
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return SecurityPerformanceSnapshot(as_of=as_of, by_symbol=by_symbol, invalid_fields=invalid_fields)
