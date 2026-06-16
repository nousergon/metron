"""Watchlist — tickers the user tracks but doesn't necessarily hold (metron-ops#42).

Positions-optional, so the product is useful with zero account data. In the no-feed beta
the watchlist is READ-ONLY / illustrative: each entry carries the symbol + reference data
(name / sector / next earnings, from the Security master) and whether it's currently
held, but NO live price — un-held tickers have no price source until the licensed Pro
feed lands. Adding a symbol caches a Security row so its reference data can resolve.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import analytics
from api.services import prices as price_service


@dataclass
class WatchlistEntry:
    symbol: str
    name: str | None
    sector: str | None
    next_earnings_date: date | None
    held: bool
    note: str | None = None


def _norm(symbol: str) -> str:
    return symbol.strip().upper()


def list_watchlist(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> list[WatchlistEntry]:
    """The portfolio's watchlist, enriched with reference data + a held flag. No price —
    the beta watchlist is illustrative (un-held tickers have no price source)."""
    items = session.scalars(
        select(models.WatchlistItem)
        .where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
        )
        .order_by(models.WatchlistItem.symbol)
    ).all()
    if not items:
        return []
    symbols = [i.symbol for i in items]
    meta = _security_meta(session, symbols)
    held_symbols = {h.ticker for h in analytics.holdings(session, tenant_id, portfolio_id)}
    out: list[WatchlistEntry] = []
    for item in items:
        name, sector, earnings = meta.get(item.symbol, (None, None, None))
        out.append(
            WatchlistEntry(
                symbol=item.symbol,
                name=name,
                sector=sector,
                next_earnings_date=earnings,
                held=item.symbol in held_symbols,
                note=item.note,
            )
        )
    return out


def add_to_watchlist(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, symbol: str, *, note: str | None = None
) -> models.WatchlistItem:
    """Add (idempotent) a symbol to the watchlist. Caches a Security row so its reference
    data can resolve. Re-adding an existing symbol updates the note only."""
    sym = _norm(symbol)
    if not sym:
        raise ValueError("symbol is required")
    price_service.ensure_security(session, sym)
    row = session.scalars(
        select(models.WatchlistItem).where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
            models.WatchlistItem.symbol == sym,
        )
    ).first()
    if row is None:
        row = models.WatchlistItem(tenant_id=tenant_id, portfolio_id=portfolio_id, symbol=sym, note=note)
        session.add(row)
    elif note is not None:
        row.note = note
    session.commit()
    session.refresh(row)
    return row


def remove_from_watchlist(
    session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID, symbol: str
) -> bool:
    """Remove a symbol from the watchlist. Returns True if a row was deleted."""
    row = session.scalars(
        select(models.WatchlistItem).where(
            models.WatchlistItem.tenant_id == tenant_id,
            models.WatchlistItem.portfolio_id == portfolio_id,
            models.WatchlistItem.symbol == _norm(symbol),
        )
    ).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def _security_meta(
    session: Session, symbols: list[str]
) -> dict[str, tuple[str | None, str | None, date | None]]:
    """``{symbol: (name, sector, next_earnings_date)}`` from the Security master."""
    rows = session.execute(
        select(
            models.Security.symbol,
            models.Security.name,
            models.Security.sector,
            models.Security.next_earnings_date,
        )
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, tuple[str | None, str | None, date | None]] = {}
    for symbol, name, sector, earnings in rows:
        out.setdefault(symbol, (name, sector, earnings))
    return out
