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

import bisect
from collections.abc import Iterable
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from api.db import models
from portfolio_analytics.prices import (
    HistorySource,
    PriceSource,
    fetch_close_history,
    fetch_latest_closes,
    fx_pair_symbol,
)


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
    # Scalar rate column + LIMIT 1: ``.first()`` does NOT add a SQL LIMIT, so a bare
    # ordered ORM query materialized EVERY on-or-before row as an FxRate object just to
    # take one — ~9s of an Accounts/Holdings-chart load, since this runs per (ccy, date)
    # across the NAV-reconstruction valuation grid. Off the (currency, base, rate_date)
    # ordering the DB returns exactly one row.
    rate = session.execute(
        select(models.FxRate.rate)
        .where(
            models.FxRate.currency == ccy,
            models.FxRate.base == base,
            models.FxRate.rate_date <= on_date,
        )
        .order_by(models.FxRate.rate_date.desc())
        .limit(1)
    ).scalar()
    return float(rate) if rate is not None else None


def latest_rate_to_base(session: Session, currency: str, *, base: str = "USD") -> float | None:
    """Most recent cached rate converting 1 unit of ``currency`` into ``base``.

    Returns 1.0 for the base currency (and empty/None input → treated as base), and
    ``None`` when no rate is cached — never a fabricated 1.0 for a real foreign
    currency."""
    base = (base or "USD").strip().upper()
    ccy = (currency or base).strip().upper()
    if ccy == base:
        return 1.0
    # Scalar rate + LIMIT 1 (see rate_as_of): never materialize the whole rate history
    # as ORM objects just to read the newest one.
    rate = session.execute(
        select(models.FxRate.rate)
        .where(models.FxRate.currency == ccy, models.FxRate.base == base)
        .order_by(models.FxRate.rate_date.desc())
        .limit(1)
    ).scalar()
    return float(rate) if rate is not None else None


class RateHistory:
    """Every cached rate for a set of currencies, read ONCE — answers :func:`rate_as_of`
    and :func:`latest_rate_to_base` for those currencies without a query per call.

    For a loop that converts at many dates. NAV reconstruction converts each foreign
    ticker at every valuation date and every lot open/close; through :func:`rate_as_of`
    that was one round trip per (currency, date) — 568 statements on one cold
    Accounts-panel load of the landing-page benchmark (three foreign currencies, five
    accounts), each a network round trip to the database. Same semantics as the two
    single-rate functions: latest rate on or before the date, ``None`` when there is none
    (never a fabricated 1.0), 1.0 for the base itself. Only currencies passed to
    :func:`rate_history` are covered — :meth:`covers` says which."""

    def __init__(self, base: str, series: dict[str, tuple[list[date], list[float]]]) -> None:
        self._base = base
        self._series = series

    def covers(self, currency: str | None) -> bool:
        ccy = (currency or self._base).strip().upper()
        return ccy == self._base or ccy in self._series

    def rate_as_of(self, currency: str | None, on_date: date) -> float | None:
        ccy = (currency or self._base).strip().upper()
        if ccy == self._base:
            return 1.0
        dates, rates = self._series.get(ccy, ([], []))
        i = bisect.bisect_right(dates, on_date)
        return rates[i - 1] if i else None

    def latest(self, currency: str | None) -> float | None:
        ccy = (currency or self._base).strip().upper()
        if ccy == self._base:
            return 1.0
        _dates, rates = self._series.get(ccy, ([], []))
        return rates[-1] if rates else None


def rate_history(session: Session, currencies: Iterable[str], *, base: str = "USD") -> RateHistory:
    """Load a :class:`RateHistory` for ``currencies`` in one query."""
    base = (base or "USD").strip().upper()
    wanted = sorted({(c or base).strip().upper() for c in currencies} - {base})
    series: dict[str, tuple[list[date], list[float]]] = {c: ([], []) for c in wanted}
    if wanted:
        rows = session.execute(
            select(models.FxRate.currency, models.FxRate.rate_date, models.FxRate.rate)
            .where(models.FxRate.currency.in_(wanted), models.FxRate.base == base)
            .order_by(models.FxRate.currency, models.FxRate.rate_date)
        ).all()
        for ccy, rate_date, rate in rows:
            dates, rates = series[ccy]
            dates.append(rate_date)
            rates.append(float(rate))
    return RateHistory(base, series)


def rates_to_base(session: Session, currencies: list[str], *, base: str = "USD") -> dict[str, float | None]:
    """Batch ``latest_rate_to_base`` — the newest cached rate of every distinct currency in
    ONE query (every valuation calls this; it was a round trip per currency). A currency
    with no cached rate maps to ``None`` (caller treats as unconvertible)."""
    base = (base or "USD").strip().upper()
    distinct = {(c or base).strip().upper() for c in currencies}
    out: dict[str, float | None] = dict.fromkeys(distinct)
    if base in out:
        out[base] = 1.0
    foreign = sorted(distinct - {base})
    if foreign:
        newer = aliased(models.FxRate)
        latest_date = (
            select(func.max(newer.rate_date))
            .where(newer.currency == models.FxRate.currency, newer.base == models.FxRate.base)
            .correlate(models.FxRate)
            .scalar_subquery()
        )
        for ccy, rate in session.execute(
            select(models.FxRate.currency, models.FxRate.rate).where(
                models.FxRate.currency.in_(foreign),
                models.FxRate.base == base,
                models.FxRate.rate_date == latest_date,
            )
        ).all():
            out[ccy] = float(rate)
    return out
