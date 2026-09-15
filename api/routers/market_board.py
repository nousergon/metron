"""Market board — technical attractiveness across Held and Watchlist scopes
(metron-ops-I304, Stage A). Brian ruling R5 (2026-09-15): the Universe scope and its
producer (alpha-engine-config-I10886) move to Stage B — this router serves ``held`` and
``watchlist`` only; any other ``scope`` value 422s rather than silently degrading to an
empty/placeholder board.

"Which stocks are technically attractive today?" (Brian, R4 constraint) — composed from
the SAME two consumers Holdings/Watchlist already read: ``api.services.technical_rating``
(intraday rating where fresh, EOD fallback otherwise, decided per symbol) and
``api.services.technical_rating_performance`` (the rating's realized track record,
metron-ops#298). Rates the SECURITY, never the holding (positioning `metron.md` §3d E8):
no ``?account_id=`` scoping, no sort by the user's portfolio weight, no buy/sell CTA —
rows sort by score alone.

Entitlement: owner (feed-entitled) build only, gated EXACTLY like the technical rating on
Holdings/Watchlist (a direct ``if not settings.feed_entitled`` 404 — the same shape as
``get_deploy_cash`` in ``portfolios.py``) — never through the ``api.entitlements`` FEATURES
catalog, which only drives the nav upsell/lock UI, not backend enforcement. The no-feed
beta 404s rather than rendering an empty board (never read as "nothing attractive").

1d/5d price change has no spine producer (only YTD/LTM do — spine-sourced, never locally
recomputed, per ``security_perf.py``'s docstring): it's computed here from the cached
``price_bars`` close history (``api.services.prices.close_history_by_symbol``), the SAME
table Holdings valuation already reads. A symbol with fewer than 2 (1d) or 6 (5d) cached
closes leaves that leg ``None`` rather than fabricated.

Deliberately a NEW router file, not an addition to ``portfolios.py`` (metron-ops-I304
binding constraint) — imports ``_owned_portfolio`` from there rather than duplicating the
tenant-ownership resolution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.db.session import get_session
from api.routers.portfolios import _owned_portfolio
from api.services import analytics
from api.services import prices as price_service
from api.services import tearsheet as tearsheet_service
from api.services import technical_rating as technical_rating_service
from api.services import technical_rating_performance as technical_rating_performance_service
from api.services import watchlist as watchlist_service

router = APIRouter(prefix="/portfolios", tags=["market-board"])

SCOPES = ("held", "watchlist")

# The SAME fixed 60-session/5d "all"-segment cell the tearsheet's per-ticker track record
# uses (metron-ops#298, tearsheet.py TRACK_RECORD_*) — mirrored here as ONE board-level
# summary line (the page header), never per row.
_TR_WINDOW = tearsheet_service.TRACK_RECORD_WINDOW
_TR_HORIZON = tearsheet_service.TRACK_RECORD_HORIZON
_TR_SEGMENT = tearsheet_service.TRACK_RECORD_SEGMENT

# 1d/5d change needs >=6 trading sessions of cached closes; a generous calendar-day
# lookback absorbs weekends/holidays without a trading-calendar dependency here.
_PRICE_LOOKBACK_DAYS = 21


@dataclass
class _PriceChange:
    change_1d_pct: float | None
    change_5d_pct: float | None


def _price_changes(session: Session, tickers: list[str], *, today: date) -> dict[str, _PriceChange]:
    """``{ticker: _PriceChange}`` from cached EOD closes (``price_bars``), ascending-date
    tail. A ticker with fewer than 2 (1d) or 6 (5d) cached closes leaves that leg ``None`` —
    never fabricated, never a stale-window average passed off as a fresh one."""
    if not tickers:
        return {}
    history = price_service.close_history_by_symbol(
        session, tickers, start_date=today - timedelta(days=_PRICE_LOOKBACK_DAYS), end_date=today
    )
    out: dict[str, _PriceChange] = {}
    for ticker in tickers:
        points = history.get(ticker) or []
        chg_1d = chg_5d = None
        if len(points) >= 2 and points[-2].close:
            chg_1d = points[-1].close / points[-2].close - 1
        if len(points) >= 6 and points[-6].close:
            chg_5d = points[-1].close / points[-6].close - 1
        out[ticker] = _PriceChange(change_1d_pct=chg_1d, change_5d_pct=chg_5d)
    return out


def _scope_tickers(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, scope: str
) -> list[tuple[str, bool]]:
    """``[(ticker, held)]`` for the requested scope. ``held`` is exactly the portfolio's
    current positions (``analytics.holdings``, all ``held=True``); ``watchlist`` is every
    tracked symbol (held or not) via the SAME watchlist service Holdings/Watchlist already
    share — ``feed_entitled=False`` here since this router needs only the symbol + held
    flag, not the full Holdings-metrics bundle ``watchlist.list_watchlist`` can enrich."""
    if scope == "held":
        return [(h.ticker, True) for h in analytics.holdings(session, tenant_id, portfolio_id)]
    entries = watchlist_service.list_watchlist(session, tenant_id, portfolio_id, feed_entitled=False)
    return [(e.symbol, e.held) for e in entries]


class MarketBoardRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    held: bool
    label: str | None  # "Technical attractiveness" (metron-ops#302 wording) — the rating label
    score: float | None
    ma_score: float | None
    osc_score: float | None
    change_1d_pct: float | None
    change_5d_pct: float | None
    basis: str | None  # "intraday" | "eod"
    as_of: str | None  # per-row — the rating's own as-of, not a board-level timestamp


class MarketBoardTrackRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment: str
    window: int
    horizon: int
    ic_mean: float | None
    noise_floor_ic: float | None
    as_of_utc: str | None


class MarketBoardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scope: str
    rows: list[MarketBoardRowOut]
    track_record: MarketBoardTrackRecordOut | None


def _board_track_record(*, reader=None) -> MarketBoardTrackRecordOut | None:
    rp = technical_rating_performance_service.load_rating_performance(reader=reader)
    if rp is None:
        return None
    stats = rp.stats_for(segment=_TR_SEGMENT, window=_TR_WINDOW, horizon=_TR_HORIZON)
    if stats is None:
        return None
    return MarketBoardTrackRecordOut(
        segment=_TR_SEGMENT,
        window=_TR_WINDOW,
        horizon=_TR_HORIZON,
        ic_mean=stats.ic_mean,
        noise_floor_ic=stats.noise_floor_ic,
        as_of_utc=rp.as_of_utc,
    )


@router.get("/{portfolio_id}/market-board", response_model=MarketBoardOut)
def get_market_board(
    scope: str = Query(..., pattern="^(held|watchlist)$"),
    portfolio: models.Portfolio = Depends(_owned_portfolio),
    session: Session = Depends(get_session),
) -> MarketBoardOut:
    """"Which stocks are technically attractive today?" across Held or Watchlist
    (metron-ops-I304 Stage A; the Universe scope and its producer move to Stage B per
    Brian ruling R5, 2026-09-15 — a ``scope`` outside ``held``/``watchlist`` 422s).

    Owner (feed-entitled) build only — the no-feed beta 404s, exactly like Deploy Cash and
    every other yfinance-derived surface (metron-ops#52): the whole board is spine-fed, so
    the beta must not learn the panel exists.

    Rates the SECURITY, never the holding: no ``?account_id=`` scoping, no sort by
    position weight — rows sort by score (attractiveness) alone, unrated symbols last.
    """
    if not settings.feed_entitled:
        raise HTTPException(status_code=404, detail="Not found")
    if scope not in SCOPES:
        raise HTTPException(status_code=422, detail="scope must be 'held' or 'watchlist'")

    tickers = _scope_tickers(session, portfolio.tenant_id, portfolio.id, scope)
    symbols = [t for t, _held in tickers]
    yf_map = tearsheet_service._yf_symbol_map(session, symbols)
    ratings = technical_rating_service.load_technical_rating().by_symbol
    today = datetime.now(UTC).date()
    changes = _price_changes(session, symbols, today=today)

    rows: list[MarketBoardRowOut] = []
    for ticker, held in tickers:
        yf = yf_map.get(ticker, ticker)
        r = ratings.get(yf)
        chg = changes.get(ticker, _PriceChange(change_1d_pct=None, change_5d_pct=None))
        rows.append(
            MarketBoardRowOut(
                symbol=ticker,
                held=held,
                label=r.label if r else None,
                score=r.score if r else None,
                ma_score=r.ma_score if r else None,
                osc_score=r.osc_score if r else None,
                change_1d_pct=chg.change_1d_pct,
                change_5d_pct=chg.change_5d_pct,
                basis=r.basis if r else None,
                as_of=r.as_of if r else None,
            )
        )
    # Sort by score descending; unrated (None) rows sort last, never mixed in as if the
    # missing score were 0/neutral.
    rows.sort(key=lambda row: (row.score is None, -(row.score or 0.0)))

    return MarketBoardOut(scope=scope, rows=rows, track_record=_board_track_record())
