"""User-set security classification overrides (GICS sector + country of domicile).

Sector + country are reference data the data spine resolves per security, but the source
can't classify everything — a numeric-CUSIP bond, an illiquid foreign listing, a fund —
leaving a holding "Unclassified" in the Allocation breakdown. This lets a tenant fill (or
correct) that gap. Tenant-scoped, keyed by symbol (how holdings are addressed); the
override overlays the tenant's view without touching the shared global ``securities`` row.

``sector`` and ``country`` are independent: a patch sets only the fields it carries (use
``UNSET`` to leave one untouched), and clearing the last remaining field deletes the row.
No fabrication elsewhere — this is the ONE place a user-asserted classification enters, and
it's explicit and per-tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models


@dataclass(frozen=True)
class Classification:
    sector: str | None = None
    country: str | None = None
    instrument_type: str | None = None


# Sentinel for "field not provided in this patch" (distinct from None = "clear this field").
UNSET = object()


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _norm_value(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def overrides_by_symbol(
    session: Session, tenant_id: uuid.UUID, symbols: list[str]
) -> dict[str, Classification]:
    """``{symbol: Classification}`` for the tenant's overrides among ``symbols`` (omits any
    symbol with no override row)."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rows = session.execute(
        select(
            models.SecurityClassification.symbol,
            models.SecurityClassification.sector,
            models.SecurityClassification.country,
            models.SecurityClassification.instrument_type,
        ).where(
            models.SecurityClassification.tenant_id == tenant_id,
            models.SecurityClassification.symbol.in_(symbols),
        )
    ).all()
    return {
        symbol: Classification(sector=sector, country=country, instrument_type=instrument_type)
        for symbol, sector, country, instrument_type in rows
    }


def set_classification(
    session: Session,
    tenant_id: uuid.UUID,
    symbol: str,
    *,
    sector: str | None | object = UNSET,
    country: str | None | object = UNSET,
    instrument_type: str | None | object = UNSET,
) -> Classification | None:
    """Upsert a tenant's sector/country/instrument_type override for ``symbol``. Fields left
    ``UNSET`` keep their stored value; a field passed as ``None``/empty CLEARS it. When all
    effective fields end up empty the row is deleted. Returns the stored ``Classification``
    (or None when cleared away)."""
    sym = _norm_symbol(symbol)
    if not sym:
        raise ValueError("symbol is required")

    row = session.scalars(
        select(models.SecurityClassification).where(
            models.SecurityClassification.tenant_id == tenant_id,
            models.SecurityClassification.symbol == sym,
        )
    ).first()

    new_sector = row.sector if (sector is UNSET and row is not None) else (
        None if sector is UNSET else _norm_value(sector)  # type: ignore[arg-type]
    )
    new_country = row.country if (country is UNSET and row is not None) else (
        None if country is UNSET else _norm_value(country)  # type: ignore[arg-type]
    )
    new_type = row.instrument_type if (instrument_type is UNSET and row is not None) else (
        None if instrument_type is UNSET else _norm_value(instrument_type)  # type: ignore[arg-type]
    )

    if new_sector is None and new_country is None and new_type is None:
        if row is not None:
            session.delete(row)
            session.commit()
        return None

    if row is None:
        row = models.SecurityClassification(
            tenant_id=tenant_id, symbol=sym, sector=new_sector, country=new_country, instrument_type=new_type
        )
        session.add(row)
    else:
        row.sector = new_sector
        row.country = new_country
        row.instrument_type = new_type
    session.commit()
    return Classification(sector=new_sector, country=new_country, instrument_type=new_type)
