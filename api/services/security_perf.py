"""Per-security period returns for the Holdings table — Day (overnight/intraday/day),
YTD, and LTM, per ticker.

- **Day legs** (overnight/intraday/day) reuse ``intraday.today_view`` (the canonical
  prior-close / open / latest decomposition from the yfinance intraday spine). Owner-build
  only (``feed_entitled``); blank otherwise — never fabricated.
- **YTD / LTM** are read from the spine ``security_performance/latest.json`` artifact
  (producer: alpha-engine-data ``collect_security_performance``). Feed-entitled only —
  Metron never recomputes these from local ``price_bars``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from krepis.trading_calendar import count_trading_days
from sqlalchemy.orm import Session

from api.services import intraday
from api.services import security_performance as performance_service
from api.services import tearsheet as tearsheet_service

# The EOD close feed is keyed by NYSE trading days, so freshness is judged in market time.
_MARKET_TZ = ZoneInfo("America/New_York")
# Flag a close-fed price as stale once it lags the latest session by this many sessions.
# 0 = priced today, 1 = priced the prior session (normal before today's close prints), so
# the first value that means "a whole session was skipped" is 2.
_STALE_AFTER_SESSIONS = 2


def sessions_behind(price_date: date, today: date) -> int:
    """How many NYSE trading sessions ``price_date`` lags ``today`` — trading sessions
    strictly after ``price_date`` through ``today`` inclusive (0 when priced today or in
    the future).

    NYSE-holiday-aware via ``krepis.trading_calendar.count_trading_days`` (the fleet-wide
    trading-day calendar, per the two-axis date doctrine, config#1610 2026-07-02): a
    Friday close read on the Monday after a normal weekend is 1 (fresh), and so is a
    Thursday close read on the Monday after a Friday NYSE holiday — the previous
    weekday-only version double-counted that holiday as a missed session and mis-flagged
    it stale (fixed 2026-07-06)."""
    return count_trading_days(price_date, today)


def market_today(now: datetime | None = None) -> date:
    """The current NYSE calendar date (market time), so a late-UTC-evening run doesn't
    roll 'today' forward a day relative to the trading session."""
    now = now or datetime.now(UTC)
    return now.astimezone(_MARKET_TZ).date()


@dataclass
class SecurityReturns:
    overnight_pct: float | None = None  # (open − prev_close) / prev_close
    intraday_pct: float | None = None   # (last − open) / open
    day_pct: float | None = None        # (last − prev_close) / prev_close
    ytd_pct: float | None = None        # vs first close on/after Jan 1
    ltm_pct: float | None = None        # vs first close on/after as_of − 1y


def index_period_returns(
    symbols: Collection[str],
    *,
    performance_reader=None,
) -> dict[str, tuple[float | None, float | None]]:
    """``{symbol: (ytd_pct, ltm_pct)}`` for index/ETF proxies from the
    ``security_performance`` spine (metron-ops#87). Symbols absent from the artifact
    are omitted — never locally recomputed from ``price_bars``."""
    snap = performance_service.load_security_performance(reader=performance_reader)
    out: dict[str, tuple[float | None, float | None]] = {}
    for sym in symbols:
        row = snap.by_symbol.get(sym)
        if row is not None:
            out[sym] = (row.ytd_pct, row.ltm_pct)
    return out


def per_security_returns(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    tickers: Collection[str],
    *,
    as_of: date,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    performance_reader=None,
    now: datetime | None = None,
) -> dict[str, SecurityReturns]:
    """``{ticker: SecurityReturns}`` for the given held tickers. YTD/LTM come from the
    security_performance spine when ``feed_entitled``; day legs from intraday when entitled."""
    out: dict[str, SecurityReturns] = {t: SecurityReturns() for t in tickers}
    if not out:
        return out

    if feed_entitled:
        snap = performance_service.load_security_performance(reader=performance_reader)
        yf_map = tearsheet_service._yf_symbol_map(session, list(out))
        for ticker, sr in out.items():
            row = snap.by_symbol.get(yf_map.get(ticker, ticker))
            if row is not None:
                sr.ytd_pct = row.ytd_pct
                sr.ltm_pct = row.ltm_pct

    # Day decomposition from the intraday spine (owner build only).
    if feed_entitled:
        today = intraday.today_view(
            session, tenant_id, portfolio_id,
            feed_entitled=True, account_ids=account_ids, reader=reader, now=now or datetime.now(UTC),
        )
        for row in today.rows:
            sr = out.get(row.ticker)
            if sr is not None:
                sr.overnight_pct = row.overnight_pct
                sr.intraday_pct = row.intraday_pct
                sr.day_pct = row.day_pct
    return out


def enrich_holdings(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    held: list,
    *,
    as_of: date,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    performance_reader=None,
    now: datetime | None = None,
) -> list:
    """Populate ``overnight_pct`` / ``intraday_pct`` / ``day_pct`` / ``ytd_pct`` /
    ``ltm_pct`` and the ``last_price_stale`` freshness flag on each ``analytics.Holding``
    in place, then return the list."""
    returns = per_security_returns(
        session, tenant_id, portfolio_id, [h.ticker for h in held],
        as_of=as_of, feed_entitled=feed_entitled, account_ids=account_ids,
        reader=reader, performance_reader=performance_reader, now=now,
    )
    today = market_today(now)
    for h in held:
        sr = returns.get(h.ticker)
        if sr is not None:
            h.overnight_pct = sr.overnight_pct
            h.intraday_pct = sr.intraday_pct
            h.day_pct = sr.day_pct
            h.ytd_pct = sr.ytd_pct
            h.ltm_pct = sr.ltm_pct
        # Stale only on the close-fed path: a broker snapshot is legitimately old and is
        # not the upstream feed stalling. An intraday-overlaid holding carries today's
        # bar_date, so it reads fresh (0 sessions behind).
        h.last_price_stale = (
            h.last_price_from_close
            and h.last_price_date is not None
            and sessions_behind(h.last_price_date, today) >= _STALE_AFTER_SESSIONS
        )
        # Positions staleness (metron-ops#150) — DISTINCT from last_price_stale: this is
        # about how current the broker-reported SHARE COUNT is (has the daily broker
        # re-sync actually run recently?), not the per-share price. A fresh close price
        # multiplied by a stale share count still produces a wrong, but fresh-LOOKING,
        # market value — this flag is what makes that failure mode visible. None
        # (h.broker_as_of is None) for ledger-only (CSV/OFX) holdings, which have no
        # broker snapshot to go stale.
        h.positions_stale = (
            h.broker_as_of is not None
            and sessions_behind(h.broker_as_of, today) >= _STALE_AFTER_SESSIONS
        )
    return held
