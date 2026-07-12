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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from portfolio_analytics.calendar import EarningsSource, fetch_earnings_dates


@dataclass
class CalendarEvent:
    event_date: date
    kind: str  # "earnings" | "release" | "fomc" (macro events — metron-ops#49)
    ticker: str  # held ticker for earnings; the series id ("UNRATE"/"FOMC") for macro
    label: str


@dataclass
class CalendarSummary:
    as_of: date
    horizon_days: int
    n_events: int = 0
    events: list[CalendarEvent] = field(default_factory=list)
    # Latest refresh_earnings() run across the held tickers; None until a refresh has run.
    earnings_sourced_at: date | None = None


def _held_tickers(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[str]:
    return [h.ticker for h in analytics.holdings(session, tenant_id, portfolio_id)]


def refresh_earnings(
    session: Session, symbols: list[str], *, source: EarningsSource | None = None, today: date | None = None
) -> int:
    """Fetch + cache the next earnings date for each of ``symbols`` onto its security.

    Overwrites (unlike sector resolution) — earnings dates move, so a refresh always
    re-sources. A symbol the source can't resolve leaves the prior cached date intact
    (and its ``earnings_sourced_at`` stamp untouched — it was NOT re-sourced this run).
    Returns the number of securities updated."""
    today = today or date.today()
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    # The spine keys earnings by yf_symbol, so fetch the Security rows first, query by
    # their yf_symbols, and map the result back per row.
    rows = session.scalars(select(models.Security).where(models.Security.symbol.in_(symbols))).all()
    if not rows:
        return 0
    yf_by_row = {row: (row.yf_symbol or row.symbol) for row in rows}
    dates = fetch_earnings_dates(sorted(set(yf_by_row.values())), source=source)
    if not dates:
        return 0
    updated = 0
    for row in rows:
        d = dates.get(yf_by_row[row])
        if d is not None:
            row.next_earnings_date = d
            row.earnings_sourced_at = today
            updated += 1
    session.commit()
    return updated


def _macro_events(today: date, end: date, source) -> list[CalendarEvent]:
    """Forward macro events (FOMC + curated FRED releases) within the horizon, from the
    spine macro artifact (metron-ops#49). Portfolio-independent. Fail-soft: a bad row is
    skipped, a missing artifact yields none."""
    out: list[CalendarEvent] = []
    for ev in source() or []:
        try:
            when = date.fromisoformat(str(ev["date"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (today <= when <= end):
            continue
        out.append(
            CalendarEvent(
                event_date=when,
                kind=str(ev.get("kind") or "release"),
                ticker=str(ev.get("series_id") or ""),
                label=str(ev.get("label") or "Macro release"),
            )
        )
    return out


def upcoming_events(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    today: date,
    horizon_days: int = 120,
    macro_events_source=None,
) -> CalendarSummary:
    """Held-ticker earnings PLUS forward macro events (FOMC + macro releases, metron-ops#49)
    within ``[today, today + horizon_days]``, sorted by date. Earnings come from the cached
    dates (POST .../calendar/refresh to populate); macro events from the spine macro
    artifact. ``macro_events_source`` is injectable for tests (defaults to the S3 read)."""
    if macro_events_source is None:
        from portfolio_analytics.macro.spine_source import spine_macro_events

        macro_events_source = spine_macro_events

    summary = CalendarSummary(as_of=today, horizon_days=horizon_days)
    end = today + timedelta(days=horizon_days)
    events: list[CalendarEvent] = []

    tickers = _held_tickers(session, tenant_id, portfolio_id)
    if tickers:
        rows = session.execute(
            select(models.Security.symbol, models.Security.next_earnings_date).where(
                models.Security.symbol.in_(tickers),
                models.Security.next_earnings_date.is_not(None),
            )
        ).all()
        seen: set[str] = set()
        for symbol, when in rows:
            if symbol in seen or when is None or not (today <= when <= end):
                continue
            seen.add(symbol)
            events.append(CalendarEvent(event_date=when, kind="earnings", ticker=symbol, label=f"{symbol} earnings"))
        summary.earnings_sourced_at = session.execute(
            select(func.max(models.Security.earnings_sourced_at)).where(models.Security.symbol.in_(tickers))
        ).scalar_one_or_none()

    # Macro events are global (not portfolio-scoped) — surfaced even with no holdings.
    events.extend(_macro_events(today, end, macro_events_source))

    events.sort(key=lambda e: (e.event_date, e.kind, e.ticker))
    summary.events = events
    summary.n_events = len(events)
    return summary
