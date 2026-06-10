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

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.prices import ClosePoint, PriceSource, fetch_latest_closes


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
