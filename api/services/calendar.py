"""Upcoming portfolio events for the Calendar page.

For now: held-ticker earnings dates, cached on ``securities.next_earnings_date`` and
refreshed on demand (yfinance, like prices/sectors — a GET reads the cache, a POST
refreshes it, so the page never fans out a slow per-ticker fetch on load). Earnings is
per-security reference data, so one fetch serves every tenant.

No fabrication: a ticker without a resolvable date is simply absent from the calendar;
events outside the horizon are filtered, not invented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from portfolio_analytics.calendar import EarningsSource, fetch_earnings_dates


@dataclass
class CalendarEvent:
    event_date: date
    kind: str  # "earnings" (FOMC / macro-release kinds deferred)
    ticker: str
    label: str


@dataclass
class CalendarSummary:
    as_of: date
    horizon_days: int
    n_events: int = 0
    events: list[CalendarEvent] = field(default_factory=list)


def _held_tickers(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[str]:
    return [h.ticker for h in analytics.holdings(session, tenant_id, portfolio_id)]


def refresh_earnings(session: Session, symbols: list[str], *, source: EarningsSource | None = None) -> int:
    """Fetch + cache the next earnings date for each of ``symbols`` onto its security.

    Overwrites (unlike sector resolution) — earnings dates move, so a refresh always
    re-sources. A symbol the source can't resolve leaves the prior cached date intact.
    Returns the number of securities updated."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    dates = fetch_earnings_dates(symbols, source=source)
    if not dates:
        return 0
    rows = session.scalars(select(models.Security).where(models.Security.symbol.in_(list(dates)))).all()
    updated = 0
    for row in rows:
        d = dates.get(row.symbol)
        if d is not None:
            row.next_earnings_date = d
            updated += 1
    session.commit()
    return updated


def upcoming_events(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    horizon_days: int = 120,
) -> CalendarSummary:
    """Held-ticker earnings within ``[today, today + horizon_days]``, from cached dates,
    sorted by date. Reads only the cache — POST .../calendar/refresh to populate it."""
    tickers = _held_tickers(session, tenant_id, portfolio_id)
    summary = CalendarSummary(as_of=today, horizon_days=horizon_days)
    if not tickers:
        return summary
    end = today + timedelta(days=horizon_days)
    rows = session.execute(
        select(models.Security.symbol, models.Security.next_earnings_date).where(
            models.Security.symbol.in_(tickers),
            models.Security.next_earnings_date.is_not(None),
        )
    ).all()
    seen: set[str] = set()
    events: list[CalendarEvent] = []
    for symbol, when in rows:
        if symbol in seen or when is None or not (today <= when <= end):
            continue
        seen.add(symbol)
        events.append(CalendarEvent(event_date=when, kind="earnings", ticker=symbol, label=f"{symbol} earnings"))
    events.sort(key=lambda e: (e.event_date, e.ticker))
    summary.events = events
    summary.n_events = len(events)
    return summary
