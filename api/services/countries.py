"""Per-security country-of-domicile resolution + cache over the global ``securities``
table, plus the US-vs-international geo classification the Holdings split needs.

Country is reference data (a property of the security, NOT tenant-scoped), so one
classification serves every tenant — exactly like ``sectors``. ``ensure_countries``
lazily fills any ``securities.country`` that's still NULL from the country source (the
data spine by default); ``countries_by_symbol`` reads the cache.

No fabrication: a symbol the source can't classify keeps ``country = NULL`` and is
surfaced as a coverage gap (an "Unclassified" geo bucket), never a guessed country.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models

# The pure bucket rule (US_COUNTRY / INTERNATIONAL / is_us_domicile / geo_bucket) lives in
# ``portfolio_analytics.sectors.geo`` (lifted for the diagnostics engine, metron-ops-I167);
# re-exported here so this service stays the API layer's one import site for geo semantics.
from portfolio_analytics.sectors import (
    INTERNATIONAL,
    US_COUNTRY,
    CountrySource,
    fetch_countries,
    geo_bucket,
    is_us_domicile,
)

__all__ = [
    "INTERNATIONAL",
    "US_COUNTRY",
    "countries_by_symbol",
    "ensure_countries",
    "geo_bucket",
    "is_us_domicile",
]


def countries_by_symbol(session: Session, symbols: list[str]) -> dict[str, str | None]:
    """Cached country of domicile per symbol (``None`` for an unclassified/unknown one)."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    rows = session.execute(
        select(models.Security.symbol, models.Security.country)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, str | None] = {}
    for symbol, country in rows:
        out.setdefault(symbol, country)  # first row per symbol wins (stable)
    return out


def ensure_countries(session: Session, symbols: list[str], *, source: CountrySource | None = None) -> int:
    """Resolve + persist the country of domicile for any of ``symbols`` whose ``securities``
    row has none yet. Idempotent — already-classified securities are left untouched, so
    re-running only sources the gaps. Returns the number of securities updated."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return 0
    rows = session.scalars(
        select(models.Security).where(
            models.Security.symbol.in_(symbols),
            models.Security.country.is_(None),
        )
    ).all()
    if not rows:
        return 0
    # The spine keys countries by yf_symbol (foreign listings exchange-suffixed), so resolve
    # symbol→yf_symbol before querying and map the result back per row.
    yf_by_row = {row: (row.yf_symbol or row.symbol) for row in rows}
    resolved = fetch_countries(sorted(set(yf_by_row.values())), source=source)
    if not resolved:
        return 0
    updated = 0
    for row in rows:
        country = resolved.get(yf_by_row[row])
        if country:
            row.country = country
            updated += 1
    session.commit()
    return updated
