"""EOD price cache over the global ``price_bars`` table.

Prices are reference data, NOT tenant-scoped: one fetch per symbol-day serves every
tenant (the cost-is-per-universe-not-per-user property). This service refreshes the
cache from a price source (yfinance by default) and reads the latest cached close per
symbol for valuation.

No fabrication: a symbol with no cached bar is simply absent from the lookup, so the
caller shows cost basis only rather than a guessed market value.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.prices import (
    ClosePoint,
    HistorySource,
    PriceSource,
    fetch_close_history,
    fetch_latest_closes,
)


def _security_ids_by_symbol(session: Session, symbols: list[str]) -> dict[str, uuid.UUID]:
    """Resolve held symbols to their global security id. A symbol with multiple
    currency listings collapses to one (personal-tier portfolios are single-currency
    per ticker in practice); the first by id wins, deterministically."""
    rows = session.execute(
        select(models.Security.symbol, models.Security.id)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, uuid.UUID] = {}
    for symbol, sec_id in rows:
        out.setdefault(symbol, sec_id)  # first id per symbol — stable
    return out


def refresh_latest_prices(session: Session, symbols: list[str], *, source: PriceSource | None = None) -> int:
    """Fetch the latest close per symbol and upsert it into ``price_bars``.

    Idempotent on (security_id, bar_date): re-refreshing the same session's close
    updates the existing bar rather than duplicating it. Only symbols that (a) have a
    ``securities`` row and (b) the source could price are written. Returns the number
    of bars upserted."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    closes = fetch_latest_closes(symbols, source=source)
    if not closes:
        return 0
    sec_by_symbol = _security_ids_by_symbol(session, list(closes))

    written = 0
    for symbol, point in closes.items():
        sec_id = sec_by_symbol.get(symbol)
        if sec_id is None:
            continue  # not a held security in this DB — nothing to value
        existing = session.scalars(
            select(models.PriceBar).where(
                models.PriceBar.security_id == sec_id,
                models.PriceBar.bar_date == point.bar_date,
            )
        ).first()
        if existing is None:
            session.add(models.PriceBar(security_id=sec_id, bar_date=point.bar_date, close=point.close))
        else:
            existing.close = point.close
        written += 1
    session.commit()
    return written


def latest_close_by_symbol(session: Session, symbols: list[str]) -> dict[str, ClosePoint]:
    """Most recent cached close per symbol, read from ``price_bars``.

    Absent symbols (never refreshed, or refreshed but unpriceable) are omitted — the
    caller treats absence as "no market value"."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.PriceBar.bar_date, models.PriceBar.close)
        .join(models.PriceBar, models.PriceBar.security_id == models.Security.id)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.PriceBar.bar_date.desc())
    ).all()
    out: dict[str, ClosePoint] = {}
    for symbol, bar_date, close in rows:
        if symbol not in out:  # rows are newest-first per symbol → first is latest
            out[symbol] = ClosePoint(bar_date=bar_date, close=float(close))
    return out


def ensure_security(session: Session, symbol: str, *, currency: str = "USD") -> uuid.UUID:
    """Get-or-create a global Security id for a symbol. Used to cache a benchmark
    (e.g. SPY) that isn't a held position so its history can live in ``price_bars``."""
    row = session.scalars(
        select(models.Security).where(models.Security.symbol == symbol).order_by(models.Security.id)
    ).first()
    if row is None:
        row = models.Security(symbol=symbol, name=symbol, currency=currency)
        session.add(row)
        session.flush()
    return row.id


def backfill_prices(
    session: Session, symbols: list[str], start: date, end: date, *, source: HistorySource | None = None
) -> int:
    """Backfill the daily close history for symbols over ``[start, end]`` into the
    cache. Idempotent: existing (security, day) bars are left as-is; only missing days
    are inserted. Symbols without a securities row (and unresolvable ones) are skipped.
    Returns the number of bars inserted."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols or start > end:
        return 0
    history = fetch_close_history(symbols, start, end, source=source)
    if not history:
        return 0
    sec_by_symbol = _security_ids_by_symbol(session, list(history))
    if not sec_by_symbol:
        return 0
    # Preload the (security, day) bars already cached for these securities so the
    # backfill is one query + plain inserts, not a select-per-day. Preload ALL dates
    # for the securities (not just [start, end]): a source may return points outside
    # the requested window, or a prior refresh may have written a bar the window
    # doesn't cover — a window-filtered preload would miss those and the insert would
    # then hit the (security, day) unique constraint.
    sec_ids = list(sec_by_symbol.values())
    existing = {
        (sid, bd)
        for sid, bd in session.execute(
            select(models.PriceBar.security_id, models.PriceBar.bar_date).where(
                models.PriceBar.security_id.in_(sec_ids),
            )
        ).all()
    }
    inserted = 0
    for symbol, series in history.items():
        sec_id = sec_by_symbol.get(symbol)
        if sec_id is None:
            continue
        for point in series:
            if (sec_id, point.bar_date) in existing:
                continue
            session.add(models.PriceBar(security_id=sec_id, bar_date=point.bar_date, close=point.close))
            existing.add((sec_id, point.bar_date))
            inserted += 1
    session.commit()
    return inserted


def close_history_by_symbol(session: Session, symbols: list[str]) -> dict[str, list[ClosePoint]]:
    """Full cached close series per symbol, ascending by date — for as-of valuation
    during reconstruction. Absent symbols are omitted."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.PriceBar.bar_date, models.PriceBar.close)
        .join(models.PriceBar, models.PriceBar.security_id == models.Security.id)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.PriceBar.bar_date)
    ).all()
    out: dict[str, list[ClosePoint]] = {}
    for symbol, bar_date, close in rows:
        out.setdefault(symbol, []).append(ClosePoint(bar_date=bar_date, close=float(close)))
    return out
