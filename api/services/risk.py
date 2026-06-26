"""Factor risk decomposition over the cached price history (C2-6c).

Ports robodashboard's investable-factor construction (VA5 Option B) onto Metron's
price cache: **Market** = SPY's daily return; **style factors** = each iShares MSCI
USA factor ETF's return minus SPY (market-neutralized spreads, so a holding's market
beta and its style tilts separate cleanly). Holding + factor return series feed
``nousergon_lib.quant.factor_risk`` for ex-ante risk decomposition + tracking error
vs SPY.

Never fabricated: a holding without enough aligned history is excluded (and named),
not regressed on a guess; with too little market history the whole result is marked
not-computable with a reason.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from nousergon_lib.quant.factor_risk import (
    benchmark_exposure,
    estimate_factor_model,
    portfolio_risk,
    tracking_error,
)
from sqlalchemy.orm import Session

from api.services import analytics
from api.services import prices as price_service
from portfolio_analytics.prices import ClosePoint, HistorySource

MARKET_ETF = "SPY"
# Style factor label → its iShares MSCI USA factor ETF (return spread over SPY).
STYLE_ETF: dict[str, str] = {
    "Momentum": "MTUM",
    "Quality": "QUAL",
    "LowVol": "USMV",
    "Value": "VLUE",
    "Size": "SIZE",
}
FACTORS = ["Market", *STYLE_ETF]
_MIN_OBS = len(FACTORS) + 5  # estimate_factor_model needs ≥ k+2; this is a sane floor


@dataclass
class RiskSummary:
    computable: bool
    benchmark: str = MARKET_ETF
    reason: str | None = None
    # Set only when not computable because the product tier / data feed excludes the
    # feature — the cheapest tier that would unlock it (drives the entitlement upsell).
    required_tier: str | None = None
    as_of: date | None = None
    n_obs: int = 0
    n_modeled: int = 0
    excluded: list[str] = field(default_factory=list)
    total_vol: float | None = None
    factor_vol: float | None = None
    idio_vol: float | None = None
    idio_pct: float | None = None
    tracking_error: float | None = None
    factor_exposures: dict[str, float] = field(default_factory=dict)
    factor_pct_contrib: dict[str, float] = field(default_factory=dict)


def _returns(series: list[ClosePoint] | None) -> pd.Series | None:
    """Daily simple returns of a close series (date-indexed), or None if absent."""
    if not series:
        return None
    closes = pd.Series({p.bar_date: p.close for p in series}).sort_index()
    return closes.pct_change().dropna()


def _factor_returns(hist: dict[str, list[ClosePoint]]) -> dict[str, pd.Series] | None:
    """Market + style-spread return series. None if SPY (the anchor) is too short."""
    spy = _returns(hist.get(MARKET_ETF))
    if spy is None or len(spy) < _MIN_OBS:
        return None
    series: dict[str, pd.Series] = {"Market": spy}
    for label, etf in STYLE_ETF.items():
        etf_ret = _returns(hist.get(etf))
        if etf_ret is None:
            continue  # missing style ETF → factor omitted, model still identifies
        aligned = pd.concat([etf_ret, spy], axis=1, join="inner")
        if len(aligned) < _MIN_OBS:
            continue
        series[label] = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    return series


def _align(
    factor_series: dict[str, pd.Series], holding_series: dict[str, pd.Series]
) -> tuple[dict[str, list[float]], dict[str, list[float]]] | None:
    """Align factors + holdings onto the common grid the factors define (port of
    robodashboard's ``align_returns``): trim trailing dates the holdings broadly lack
    (today's forming factor-ETF bar), then keep only holdings present on every grid
    date (a recent buy can't be regressed over the full window — it's excluded)."""
    fframe = pd.DataFrame(factor_series).dropna()
    if len(fframe) < _MIN_OBS:
        return None
    idx = fframe.index
    if holding_series:
        need = max(1, math.ceil(0.8 * len(holding_series)))
        hold_idxs = [s.index for s in holding_series.values()]
        well_covered = idx[[sum(d in hi for hi in hold_idxs) >= need for d in idx]]
        if len(well_covered):
            idx = idx[idx <= well_covered.max()]
        if len(idx) < _MIN_OBS:
            return None
        fframe = fframe.loc[idx]

    holdings: dict[str, list[float]] = {}
    for ticker, ret in holding_series.items():
        aligned = ret.reindex(idx)
        if aligned.notna().all():
            holdings[ticker] = aligned.to_numpy(dtype=float).tolist()
    if not holdings:
        return None
    factors = {f: fframe[f].to_numpy(dtype=float).tolist() for f in fframe.columns}
    return factors, holdings


def compute_risk(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    window_days: int = 252,
    do_backfill: bool = False,
    source: HistorySource | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> RiskSummary:
    """Ex-ante factor risk of the market-value-weighted portfolio. ``do_backfill``
    fetches the held + factor-ETF history over the window first (the POST path); the
    GET path computes from whatever is already cached. ``account_ids`` scopes the
    holdings (weights + backfilled tickers) to the selected accounts; None = whole
    portfolio (factor-ETF history is global and stays unscoped)."""
    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    priced = [h for h in held if h.market_value and h.market_value > 0]
    if not priced:
        return RiskSummary(False, reason="No priced holdings — refresh prices first.")
    total_mv = sum(h.market_value for h in priced)
    weights = {h.ticker: h.market_value / total_mv for h in priced}
    tickers = list(weights)
    etfs = [MARKET_ETF, *STYLE_ETF.values()]

    if do_backfill:
        start = today - timedelta(days=int(window_days * 1.6))  # ~window_days trading sessions
        for etf in etfs:
            price_service.ensure_security(session, etf)
        price_service.backfill_prices(session, [*tickers, *etfs], start, today, source=source)

    hist = price_service.close_history_by_symbol(session, [*tickers, *etfs])
    factor_series = _factor_returns(hist)
    if factor_series is None:
        return RiskSummary(False, reason="Not enough market history yet — compute risk to backfill it.")
    holding_series = {t: r for t in tickers if (r := _returns(hist.get(t))) is not None and len(r) >= _MIN_OBS}
    aligned = _align(factor_series, holding_series)
    if aligned is None:
        return RiskSummary(False, reason="Too few aligned observations to fit the model.")
    factor_returns, holding_returns = aligned

    model = estimate_factor_model(holding_returns, factor_returns)
    risk = portfolio_risk(model, weights)
    bench = benchmark_exposure(factor_returns["Market"], factor_returns)
    te = tracking_error(model, weights, bench)
    modeled = set(holding_returns)
    return RiskSummary(
        computable=True,
        as_of=today,
        n_obs=len(factor_returns["Market"]),
        n_modeled=len(modeled),
        excluded=sorted(t for t in tickers if t not in modeled),
        total_vol=risk["total_vol"],
        factor_vol=risk["factor_vol"],
        idio_vol=risk["idio_vol"],
        idio_pct=risk["idio_pct"],
        tracking_error=te["tracking_error"],
        factor_exposures=risk["factor_exposures"],
        factor_pct_contrib=risk["factor_pct_contrib"],
    )
