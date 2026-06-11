"""FX rate cache over the global ``fx_rates`` table.

Foreign holdings are valued in their native currency (HKD, GBP, …); the portfolio
base is USD. This service caches the conversion rate (USD per 1 unit of the quote
currency) so valuation can fold native market values into one base-currency total.

Like ``prices``, rates are reference data — one fetch of ``HKDUSD=X`` serves every
tenant. Source-agnostic: it reuses the injectable yfinance price source (an FX pair
is just another symbol), so the licensed-feed swap in the public tier is free.

**No fabrication.** A currency whose rate we cannot source is simply absent from the
lookup (returns ``None``) — the caller then leaves that holding's *base* value unset
and shows the native amount instead, rather than silently treating 1 HKD as 1 USD.
USD→USD is the identity 1.0 and is never fetched or stored.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.prices import HistorySource, PriceSource, fetch_close_history, fetch_latest_closes, fx_pair_symbol


def refresh_fx_rates(
    session: Session, currencies: list[str], *, base: str = "USD", source: PriceSource | None = None
) -> int:
    """Fetch the latest ``{CCY}{BASE}=X`` rate for each non-base currency and upsert it
    into ``fx_rates``. Idempotent on (currency, rate_date). Returns the number of rates
    upserted (the base currency and unresolvable pairs contribute nothing)."""
    base = (base or "USD").strip().upper()
    wanted = {c.strip().upper() for c in currencies if c and c.strip().upper() != base}
    if not wanted:
        return 0
    pair_to_ccy = {fx_pair_symbol(c, base): c for c in wanted}
    pair_to_ccy.pop("", None)
    if not pair_to_ccy:
        return 0
    closes = fetch_latest_closes(list(pair_to_ccy), source=source)
    if not closes:
        return 0

    written = 0
    for pair, point in closes.items():
        ccy = pair_to_ccy.get(pair)
        if ccy is None or point.close <= 0:
            continue
        existing = session.scalars(
            select(models.FxRate).where(
                models.FxRate.currency == ccy,
                models.FxRate.base == base,
                models.FxRate.rate_date == point.bar_date,
            )
        ).first()
        if existing is None:
            session.add(
                models.FxRate(currency=ccy, base=base, rate_date=point.bar_date, rate=point.close)
            )
        else:
            existing.rate = point.close
        written += 1
    session.commit()
    return written


def backfill_fx_rates(
    session: Session,
    currencies: list[str],
    start: date,
    end: date,
    *,
    base: str = "USD",
    source: HistorySource | None = None,
) -> int:
    """Backfill the daily ``{CCY}{BASE}=X`` history over ``[start, end]`` into
    ``fx_rates`` — the as-of rates that convert *historical* realized gains / dividends
    at the rate on their event date (not today's). Idempotent: existing (currency, day)
    rows are left as-is; only missing days are inserted. Returns the number inserted."""
    base = (base or "USD").strip().upper()
    wanted = {c.strip().upper() for c in currencies if c and c.strip().upper() != base}
    if not wanted or start > end:
        return 0
    pair_to_ccy = {fx_pair_symbol(c, base): c for c in wanted}
    pair_to_ccy.pop("", None)
    if not pair_to_ccy:
        return 0
    history = fetch_close_history(list(pair_to_ccy), start, end, source=source)
    if not history:
        return 0
    # Preload ALL cached (currency, day) rows for these currencies — one query + plain
    # inserts, mirroring prices.backfill_prices (avoids the per-day unique-constraint hit).
    existing = {
        (ccy, d)
        for ccy, d in session.execute(
            select(models.FxRate.currency, models.FxRate.rate_date).where(
                models.FxRate.currency.in_(wanted), models.FxRate.base == base
            )
        ).all()
    }
    inserted = 0
    for pair, series in history.items():
        ccy = pair_to_ccy.get(pair)
        if ccy is None:
            continue
        for point in series:
            if point.close <= 0 or (ccy, point.bar_date) in existing:
                continue
            session.add(models.FxRate(currency=ccy, base=base, rate_date=point.bar_date, rate=point.close))
            existing.add((ccy, point.bar_date))
            inserted += 1
    session.commit()
    return inserted


def rate_as_of(session: Session, currency: str, on_date: date, *, base: str = "USD") -> float | None:
    """The cached rate converting 1 unit of ``currency`` into ``base`` **as of**
    ``on_date`` — the latest rate on or before that date (carry-forward over weekends /
    holidays / gaps). Returns 1.0 for the base currency and ``None`` when no rate on or
    before ``on_date`` is cached (no fabrication)."""
    base = (base or "USD").strip().upper()
    ccy = (currency or base).strip().upper()
    if ccy == base:
        return 1.0
    row = session.scalars(
        select(models.FxRate)
        .where(
            models.FxRate.currency == ccy,
            models.FxRate.base == base,
            models.FxRate.rate_date <= on_date,
        )
        .order_by(models.FxRate.rate_date.desc())
    ).first()
    return float(row.rate) if row is not None else None


def latest_rate_to_base(session: Session, currency: str, *, base: str = "USD") -> float | None:
    """Most recent cached rate converting 1 unit of ``currency`` into ``base``.

    Returns 1.0 for the base currency (and empty/None input → treated as base), and
    ``None`` when no rate is cached — never a fabricated 1.0 for a real foreign
    currency."""
    base = (base or "USD").strip().upper()
    ccy = (currency or base).strip().upper()
    if ccy == base:
        return 1.0
    row = session.scalars(
        select(models.FxRate)
        .where(models.FxRate.currency == ccy, models.FxRate.base == base)
        .order_by(models.FxRate.rate_date.desc())
    ).first()
    return float(row.rate) if row is not None else None


def rates_to_base(session: Session, currencies: list[str], *, base: str = "USD") -> dict[str, float | None]:
    """Batch ``latest_rate_to_base`` — one lookup per distinct currency. A currency with
    no cached rate maps to ``None`` (caller treats as unconvertible)."""
    base = (base or "USD").strip().upper()
    distinct = {(c or base).strip().upper() for c in currencies}
    return {ccy: latest_rate_to_base(session, ccy, base=base) for ccy in distinct}
