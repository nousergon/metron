"""Per-security GICS sector resolution + cache over the global ``securities`` table.

Sector is reference data (a property of the security, NOT tenant-scoped), so one
classification serves every tenant — the same cost-per-universe property prices have.
``ensure_sectors`` lazily fills any ``securities.sector`` that's still NULL from the
sector source (yfinance by default); ``sectors_by_symbol`` reads the cache.

No fabrication: a symbol the source can't classify keeps ``sector = NULL`` and is
surfaced as an attribution-coverage gap, never assigned a guessed sector.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.sectors import SectorSource, fetch_sectors


def sectors_by_symbol(session: Session, symbols: list[str]) -> dict[str, str | None]:
    """Cached GICS sector per symbol (``None`` for an unclassified or unknown one)."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.Security.sector)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, str | None] = {}
    for symbol, sector in rows:
        out.setdefault(symbol, sector)  # first row per symbol wins (stable)
    return out


def ensure_sectors(session: Session, symbols: list[str], *, source: SectorSource | None = None) -> int:
    """Resolve + persist the GICS sector for any of ``symbols`` whose ``securities``
    row has none yet. Idempotent — already-classified securities are left untouched, so
    re-running only sources the gaps. Returns the number of securities updated."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    rows = session.scalars(
        select(models.Security).where(
            models.Security.symbol.in_(symbols),
            models.Security.sector.is_(None),
        )
    ).all()
    if not rows:
        return 0
    resolved = fetch_sectors([r.symbol for r in rows], source=source)
    if not resolved:
        return 0
    updated = 0
    for row in rows:
        sector = resolved.get(row.symbol)
        if sector:
            row.sector = sector
            updated += 1
    session.commit()
    return updated
