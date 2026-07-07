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

from sqlalchemy import func, select
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


def _securities_by_symbol(session: Session, symbols: list[str]) -> dict[str, models.Security]:
    """Resolve held symbols to their global Security row (first by id per symbol, like
    ``_security_ids_by_symbol``). Carries ``currency`` + ``yf_symbol`` so the fetch can
    use the yfinance-shaped symbol (``1299`` → ``1299.HK``) and the cached bar can be
    stamped with the instrument's native currency rather than a USD default."""
    rows = session.scalars(
        select(models.Security)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, models.Security] = {}
    for row in rows:
        out.setdefault(row.symbol, row)  # first row per symbol — stable
    return out


def _yf_symbol(sec: models.Security) -> str:
    """The symbol to price ``sec`` under — its stored ``yf_symbol`` (foreign listings
    carry an exchange suffix) falling back to the bare symbol (US/USD)."""
    return sec.yf_symbol or sec.symbol


def refresh_latest_prices(session: Session, symbols: list[str], *, source: PriceSource | None = None) -> int:
    """Fetch the latest close per symbol and upsert it into ``price_bars``.

    Fetches under each security's ``yf_symbol`` (so foreign listings like ``1299.HK``
    resolve), then writes the bar back against the stored symbol's security, stamped
    with that security's native currency. Idempotent on (security_id, bar_date). Only
    symbols that (a) have a ``securities`` row and (b) the source could price are
    written. Returns the number of bars upserted."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    secs = _securities_by_symbol(session, symbols)
    if not secs:
        return 0
    # yfinance-shaped symbol → Security (collapse duplicates, first wins).
    fetch_targets: dict[str, models.Security] = {}
    for sec in secs.values():
        fetch_targets.setdefault(_yf_symbol(sec), sec)
    closes = fetch_latest_closes(list(fetch_targets), source=source)
    if not closes:
        return 0

    written = 0
    for yf_sym, point in closes.items():
        sec = fetch_targets.get(yf_sym)
        if sec is None:
            continue  # not a held security in this DB — nothing to value
        existing = session.scalars(
            select(models.PriceBar).where(
                models.PriceBar.security_id == sec.id,
                models.PriceBar.bar_date == point.bar_date,
            )
        ).first()
        if existing is None:
            session.add(
                models.PriceBar(
                    security_id=sec.id, bar_date=point.bar_date, close=point.close, currency=sec.currency
                )
            )
        else:
            existing.close = point.close
            existing.currency = sec.currency
        written += 1
    session.commit()
    return written


def latest_close_by_symbol(session: Session, symbols: list[str]) -> dict[str, ClosePoint]:
    """Most recent cached close per symbol, read from ``price_bars``.

    Absent symbols (never refreshed, or refreshed but unpriceable) are omitted — the
    caller treats absence as "no market value".

    Latest-per-symbol via a window function (``ROW_NUMBER`` partitioned by symbol, newest
    bar first), so the DB returns ONE row per symbol off the ``(security_id, bar_date)``
    index — NOT every bar for every symbol pulled across the wire to be deduped in Python
    (that scanned ~all of ``price_bars`` on every valuation, the dominant page-load cost)."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rn = func.row_number().over(
        partition_by=models.Security.symbol,
        order_by=models.PriceBar.bar_date.desc(),
    ).label("rn")
    ranked = (
        select(
            models.Security.symbol.label("symbol"),
            models.PriceBar.bar_date.label("bar_date"),
            models.PriceBar.close.label("close"),
            rn,
        )
        .join(models.PriceBar, models.PriceBar.security_id == models.Security.id)
        .where(models.Security.symbol.in_(symbols))
        .subquery()
    )
    rows = session.execute(
        select(ranked.c.symbol, ranked.c.bar_date, ranked.c.close).where(ranked.c.rn == 1)
    ).all()
    return {
        symbol: ClosePoint(bar_date=bar_date, close=float(close)) for symbol, bar_date, close in rows
    }


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
    cache. Idempotent: missing (security, day) bars are inserted; existing bars whose
    close differs from the source (e.g. a post-split spine refresh correcting stale
    pre-split closes) are updated. Symbols without a securities row (and unresolvable
    ones) are skipped. Returns the number of bars inserted or updated."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols or start > end:
        return 0
    secs = _securities_by_symbol(session, symbols)
    if not secs:
        return 0
    fetch_targets: dict[str, models.Security] = {}
    for sec in secs.values():
        fetch_targets.setdefault(_yf_symbol(sec), sec)
    history = fetch_close_history(list(fetch_targets), start, end, source=source)
    if not history:
        return 0
    # Preload existing (security, day) bars so the backfill is one query + plain
    # inserts/updates, not a select-per-day. Preload ALL dates for the securities (not
    # just [start, end]): a source may return points outside the requested window.
    sec_ids = [sec.id for sec in fetch_targets.values()]
    existing_rows = session.execute(
        select(models.PriceBar).where(models.PriceBar.security_id.in_(sec_ids))
    ).scalars().all()
    by_key: dict[tuple[uuid.UUID, date], models.PriceBar] = {
        (row.security_id, row.bar_date): row for row in existing_rows
    }
    written = 0
    for yf_sym, series in history.items():
        sec = fetch_targets.get(yf_sym)
        if sec is None:
            continue
        for point in series:
            key = (sec.id, point.bar_date)
            row = by_key.get(key)
            if row is None:
                row = models.PriceBar(
                    security_id=sec.id, bar_date=point.bar_date, close=point.close, currency=sec.currency
                )
                session.add(row)
                by_key[key] = row
                written += 1
            elif float(row.close) != float(point.close):
                row.close = point.close
                row.currency = sec.currency
                written += 1
    session.commit()
    return written


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
