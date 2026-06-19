"""Live intraday revaluation — fresh NAV from current position balances (metron-ops#79).

The Overview / Holdings / Performance views value positions from the latest EOD close by
default. During regular trading hours, this service overlays the **intraday** last-price
per held ticker (from the alpha-engine-data data spine,
``market_data/intraday/latest.json`` — the same artifact the Markets strip reads, but its
per-held-ticker ``quotes`` map rather than the index proxies) so each position revalues
live and the headline NAV = Σ of the fresh balances, recomputed on every ~5-min poll while
Metron is open.

Display-only by design: only the page-serving (read) endpoints pass these prices into
``analytics.valued_holdings`` / ``analytics.summary``. The persisted daily NAV-history
snapshot always uses the EOD close (``valued_holdings`` with no override), so intraday
ticks never enter the recorded history.

Feed-gated: the intraday quotes are yfinance-derived (licensed), so the overlay applies
only on a feed-entitled deployment (the owner build); the no-feed beta falls back to EOD
close. A stale / missing / suspect quote falls back per-symbol — never fabricated.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from portfolio_analytics.prices import ClosePoint

logger = logging.getLogger(__name__)

INTRADAY_KEY = "market_data/intraday/latest.json"
# Match the Markets strip: a snapshot older than this is "stale" — the market closed or the
# demand-gated feed paused — so we fall back to EOD close rather than imply a live tick.
STALE_AFTER_SECONDS = 20 * 60


@dataclass
class IntradayMeta:
    """Whether the live overlay was applied, and how fresh it is (drives the UI label)."""

    applied: bool
    as_of_utc: str | None = None  # ISO8601 Z — the producer's write time
    stale: bool = False
    n_priced: int = 0             # held tickers that got an intraday last-price
    reason: str | None = None     # why not applied ("feed" / "stale" / "unavailable")


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=INTRADAY_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: degrade to EOD valuation, never break the page
        logger.warning("data-spine read failed %s: %s", INTRADAY_KEY, e)
        return None


def _is_stale(as_of_utc: str | None, now: datetime) -> bool:
    if not as_of_utc:
        return True
    try:
        beat = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - beat).total_seconds() > STALE_AFTER_SECONDS


def load_quotes(*, reader=None, now: datetime | None = None) -> tuple[dict[str, dict], str | None, bool]:
    """The latest intraday per-ticker quotes (keyed by yf_symbol) → ``(quotes, as_of_utc,
    stale)``. ``reader`` (no-arg → raw artifact dict) and ``now`` are injectable for tests."""
    now = now or datetime.now(UTC)
    art = (reader or _default_reader)()
    if not art:
        return {}, None, True
    quotes = art.get("quotes")
    as_of = art.get("as_of_utc")
    if not isinstance(quotes, dict):
        return {}, as_of, _is_stale(as_of, now)
    return quotes, as_of, _is_stale(as_of, now)


def _yf_symbol_by_ticker(session: Session, tickers: list[str]) -> dict[str, str]:
    """Held ticker → its yf_symbol (the key the intraday artifact uses), falling back to
    the bare ticker. First Security row per symbol wins (stable)."""
    if not tickers:
        return {}
    rows = session.scalars(
        select(models.Security)
        .where(models.Security.symbol.in_(tickers))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(row.symbol, row.yf_symbol or row.symbol)
    # Tickers without a Security row still map to themselves (US/USD plain symbols).
    for t in tickers:
        out.setdefault(t, t)
    return out


def _overlay(session: Session, tickers: list[str], quotes: dict[str, dict], *, today: date) -> dict[str, ClosePoint]:
    """``{ticker: ClosePoint(last)}`` for held tickers with a usable intraday quote.

    A suspect-flagged quote (producer's >40% move guard) or a missing/None ``last`` is
    skipped, so that symbol keeps its EOD close — never an intraday outlier."""
    yf_by_ticker = _yf_symbol_by_ticker(session, tickers)
    out: dict[str, ClosePoint] = {}
    for ticker in tickers:
        q = quotes.get(yf_by_ticker.get(ticker, ticker))
        if not isinstance(q, dict) or q.get("suspect"):
            continue
        last = q.get("last")
        if last is None:
            continue
        try:
            close = float(last)
        except (TypeError, ValueError):
            continue
        when = today
        sd = q.get("session_date")
        if sd:
            try:
                when = date.fromisoformat(str(sd))
            except ValueError:
                pass
        out[ticker] = ClosePoint(bar_date=when, close=close)
    return out


def live_prices(
    session: Session,
    tickers: Collection[str],
    *,
    feed_entitled: bool,
    reader=None,
    now: datetime | None = None,
    today: date | None = None,
) -> tuple[dict[str, ClosePoint] | None, IntradayMeta]:
    """A price map (``{ticker: ClosePoint}``, intraday last overlaid on EOD close) for the
    live valuation, plus an :class:`IntradayMeta`. Returns ``(None, meta)`` when the
    overlay doesn't apply (no feed entitlement / stale / no usable quote) — the caller then
    values from EOD close exactly as before.

    ``feed_entitled`` is the deployment's feed axis (licensed-quote display); ``reader`` /
    ``now`` / ``today`` are injectable for tests."""
    now = now or datetime.now(UTC)
    today = today or now.date()
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not feed_entitled:
        return None, IntradayMeta(applied=False, reason="feed")
    if not tickers:
        return None, IntradayMeta(applied=False, reason="unavailable")
    quotes, as_of, stale = load_quotes(reader=reader, now=now)
    if not quotes:
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=stale, reason="unavailable")
    if stale:
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=True, reason="stale")
    overlay = _overlay(session, tickers, quotes, today=today)
    if not overlay:
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=stale, reason="unavailable")
    # EOD close is the baseline; the fresh intraday last overrides it per symbol. Symbols
    # without an intraday quote keep their close (or the broker-native fallback downstream).
    from api.services import prices as price_service

    merged = dict(price_service.latest_close_by_symbol(session, tickers))
    merged.update(overlay)
    return merged, IntradayMeta(applied=True, as_of_utc=as_of, stale=False, n_priced=len(overlay))


def for_portfolio(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    now: datetime | None = None,
) -> tuple[dict[str, ClosePoint] | None, IntradayMeta]:
    """``(prices, meta)`` for a portfolio's held tickers — the live price override to pass
    into ``valued_holdings`` / ``summary`` plus the overlay status for the UI label. The
    page endpoints use ``prices``; the ``GET .../intraday`` status endpoint uses ``meta``."""
    from api.services import analytics

    held = analytics.holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    tickers = [h.ticker for h in held if h.ticker]
    return live_prices(session, tickers, feed_entitled=feed_entitled, reader=reader, now=now)


# ── Today view: prior-close / open / latest + overnight·intraday·day decomposition ──────
# (metron-ops#23) Day % (close→close) = Overnight % (open vs prior close) + Intraday %
# (latest vs open); the $ legs are shares × the native price delta, FX-converted to base.


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class TodayRow:
    ticker: str
    label: str
    quantity: float
    currency: str
    prev_close: float | None      # native
    open: float | None            # native
    last: float | None            # native (~15-min delayed)
    overnight_pct: float | None   # (open − prev_close) / prev_close
    intraday_pct: float | None    # (last − open) / open
    day_pct: float | None         # (last − prev_close) / prev_close
    overnight_gain: float | None  # base $ = qty × (open − prev_close) × fx
    intraday_gain: float | None   # base $ = qty × (last − open) × fx
    day_gain: float | None        # base $ = qty × (last − prev_close) × fx


@dataclass
class TodaySummary:
    available: bool
    base_currency: str = "USD"
    reason: str | None = None       # "feed" / "stale" / "unavailable" when not available
    as_of_utc: str | None = None
    stale: bool = False
    n_priced: int = 0               # holdings with a usable (prev/open/last) quote
    n_excluded: int = 0             # held but un-decomposable (no quote / no FX)
    overnight_gain: float | None = None  # portfolio base $ legs
    intraday_gain: float | None = None
    day_gain: float | None = None
    overnight_pct: float | None = None   # leg $ / prior-close MV (decomposable rows)
    intraday_pct: float | None = None
    day_pct: float | None = None
    rows: list[TodayRow] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.rows is None:
            self.rows = []


def today_view(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    now: datetime | None = None,
) -> TodaySummary:
    """Per-holding prior-close / open / latest with the overnight·intraday·day P&L
    decomposition + portfolio totals, from the intraday spine quotes (metron-ops#23).

    Feed-gated (owner build); ``stale`` when the snapshot is older than the freshness
    window (market closed) — the rows still render "as of close". A holding without a
    usable quote (missing prev/open/last, suspect, or no cached FX) is excluded + counted,
    never fabricated. ``account_ids`` scopes the holdings like every other page."""
    from api.services import analytics

    now = now or datetime.now(UTC)
    if not feed_entitled:
        return TodaySummary(available=False, reason="feed")

    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    base = analytics._base_currency(session, portfolio_id)
    if not held:
        return TodaySummary(available=True, base_currency=base, reason="unavailable")

    quotes, as_of, stale = load_quotes(reader=reader, now=now)
    if not quotes:
        return TodaySummary(available=False, base_currency=base, reason="unavailable", as_of_utc=as_of, stale=True)

    yf_by_ticker = _yf_symbol_by_ticker(session, [h.ticker for h in held])
    rows: list[TodayRow] = []
    tot_prev_mv = tot_on = tot_id = tot_day = 0.0
    excluded = 0
    for h in held:
        q = quotes.get(yf_by_ticker.get(h.ticker, h.ticker))
        prev = _f(q, "prev_close") if isinstance(q, dict) else None
        opn = _f(q, "open") if isinstance(q, dict) else None
        last = _f(q, "last") if isinstance(q, dict) else None
        fx = h.fx_rate if h.fx_rate is not None else (1.0 if (h.currency or "USD") == base else None)
        suspect = bool(q.get("suspect")) if isinstance(q, dict) else False
        if suspect or prev is None or opn is None or last is None or not prev or not opn or fx is None:
            excluded += 1
            continue
        qty = h.quantity
        on_g = qty * (opn - prev) * fx
        id_g = qty * (last - opn) * fx
        day_g = qty * (last - prev) * fx
        rows.append(
            TodayRow(
                ticker=h.ticker,
                label=h.user_label or h.ticker,
                quantity=qty,
                currency=h.currency or base,
                prev_close=prev,
                open=opn,
                last=last,
                overnight_pct=(opn - prev) / prev,
                intraday_pct=(last - opn) / opn,
                day_pct=(last - prev) / prev,
                overnight_gain=on_g,
                intraday_gain=id_g,
                day_gain=day_g,
            )
        )
        tot_prev_mv += qty * prev * fx
        tot_on += on_g
        tot_id += id_g
        tot_day += day_g

    rows.sort(key=lambda r: abs(r.day_gain or 0.0), reverse=True)
    has = bool(rows)

    def pct(g: float) -> float | None:
        return (g / tot_prev_mv) if tot_prev_mv else None

    return TodaySummary(
        available=True,
        base_currency=base,
        as_of_utc=as_of,
        stale=stale,
        n_priced=len(rows),
        n_excluded=excluded,
        overnight_gain=tot_on if has else None,
        intraday_gain=tot_id if has else None,
        day_gain=tot_day if has else None,
        overnight_pct=pct(tot_on) if has else None,
        intraday_pct=pct(tot_id) if has else None,
        day_pct=pct(tot_day) if has else None,
        rows=rows,
    )
