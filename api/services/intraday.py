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
import threading
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import models
from api.services import fund_proxy
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
    # Held tickers in scope for the overlay — ``n_priced``/``n_total`` is the live COVERAGE.
    # The per-symbol merge silently keeps the EOD close for any un-quoted symbol, so a
    # partially-live NAV must be disclosed by the UI rather than read as fully live.
    n_total: int = 0
    reason: str | None = None     # why not applied ("feed" / "stale" / "unavailable")
    # Held tickers whose intraday price was SYNTHESIZED from a tracking-proxy ETF's
    # same-day return rather than read from the ticker's own intraday quote — the
    # late-striking-fund same-day ESTIMATE (metron-ops#112, mechanism B). Empty unless a
    # held ticker is a known mutual fund (``fund_proxy.FUND_PROXY``) with no usable
    # intraday quote of its own. Drives the "estimated" UI badge; restated by mechanism A
    # once the fund's true NAV lands (next data run).
    estimated_tickers: frozenset[str] = frozenset()


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


# Process-level TTL cache for the intraday snapshot S3 read. A single page load fans out to
# ~5–7 endpoints (Holdings + Accounts + Today + intraday-legs + per-security day legs), each
# of which reads the SAME ``latest.json`` artifact — so without this the snapshot was fetched
# from S3 5–7× per render, serially, dominating the Holdings page latency. The producer writes
# every ~5 min and the snapshot's own staleness window is 20 min (STALE_AFTER_SECONDS), so a
# short TTL well under both collapses the fan-out to one read while never serving a snapshot
# materially older than an un-cached read would. Freshness is still judged per-call in
# ``load_quotes`` against live ``now`` (the cache holds the artifact, not a freshness verdict),
# so a cached-but-aged snapshot still correctly reports ``stale``. Mirrors the monotonic-throttle
# pattern in ``data_spine.touch_ui_heartbeat``. The ``reader`` injection path bypasses this
# entirely, so tests are unaffected.
_SNAPSHOT_TTL_S = 30.0
_snapshot_lock = threading.Lock()
_snapshot_cache: dict | None = None
_snapshot_fetched_monotonic: float = 0.0


def _read_snapshot_s3() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=INTRADAY_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: degrade to EOD valuation, never break the page
        logger.warning("data-spine read failed %s: %s", INTRADAY_KEY, e)
        return None


def _default_reader() -> dict | None:
    """The cached intraday snapshot dict (or None on read failure). At most one S3 read per
    ``_SNAPSHOT_TTL_S`` across all consumers in this process; concurrent callers within the
    window share the cached value. A failed read is also cached for the window so a transient
    S3 blip during a page load doesn't trigger 5–7 retries (recovery lag ≤ TTL is acceptable
    — fail-soft already degrades to EOD close)."""
    global _snapshot_cache, _snapshot_fetched_monotonic
    with _snapshot_lock:
        if time.monotonic() - _snapshot_fetched_monotonic < _SNAPSHOT_TTL_S:
            return _snapshot_cache
        _snapshot_cache = _read_snapshot_s3()
        _snapshot_fetched_monotonic = time.monotonic()
        return _snapshot_cache


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


def _overlay(
    session: Session,
    tickers: list[str],
    quotes: dict[str, dict],
    *,
    today: date,
    eod_closes: dict[str, ClosePoint] | None = None,
    reader=None,
    now: datetime | None = None,
) -> tuple[dict[str, ClosePoint], set[str]]:
    """``({ticker: ClosePoint(last)}, estimated_tickers)`` for held tickers with a usable
    intraday quote, PLUS a same-day ESTIMATE for a late-striking mutual fund that has none.

    A suspect-flagged quote (producer's >40% move guard) or a missing/None ``last`` is
    skipped, so that symbol keeps its EOD close — never an intraday outlier.

    Late-striking-fund estimate (metron-ops#112, mechanism B): a mutual fund prints its
    NAV once a day, hours after Metron's EOD close run, so on any given session it has NO
    usable intraday quote of its own and would otherwise read flat all day. For a held
    ticker with no usable quote AND known to be such a fund (``ticker.upper() in
    fund_proxy.FUND_PROXY`` — the authoritative signal; no DB security_type lookup), we
    synthesize ``estimated_price = fund_eod_close * (1 + proxy_return)`` from its own
    latest EOD close (``eod_closes``) and its tracking-proxy ETF's same-day return
    (``indices.proxy_day_return``). Skipped (left un-overlaid, falling back to EOD close)
    if the proxy return or the fund's EOD close isn't available — never fabricated.
    ``estimated_tickers`` names every ticker that got this synthesized (not real-quote)
    price, so the caller can flag it "estimated" in the UI."""
    from api.services import indices as indices_service

    yf_by_ticker = _yf_symbol_by_ticker(session, tickers)
    eod_closes = eod_closes or {}
    out: dict[str, ClosePoint] = {}
    estimated: set[str] = set()
    for ticker in tickers:
        q = quotes.get(yf_by_ticker.get(ticker, ticker))
        usable = isinstance(q, dict) and not q.get("suspect") and q.get("last") is not None
        if usable:
            try:
                close = float(q["last"])
            except (TypeError, ValueError):
                usable = False
        if usable:
            when = today
            sd = q.get("session_date")
            if sd:
                try:
                    when = date.fromisoformat(str(sd))
                except ValueError:
                    pass
            out[ticker] = ClosePoint(bar_date=when, close=close)
            continue
        # No usable intraday quote of its own — estimate iff it's a known late-striking fund.
        if ticker.upper() not in fund_proxy.FUND_PROXY:
            continue
        eod = eod_closes.get(ticker)
        if eod is None or eod.close is None:
            continue
        proxy_return = indices_service.proxy_day_return(
            fund_proxy.proxy_for(ticker), reader=reader, now=now
        )
        if proxy_return is None:
            continue
        estimated_price = eod.close * (1 + proxy_return)
        out[ticker] = ClosePoint(bar_date=today, close=estimated_price)
        estimated.add(ticker)
    return out, estimated


def intraday_enabled(session: Session, tenant_id: uuid.UUID, portfolio_id: uuid.UUID) -> bool:
    """The portfolio's single user-facing intraday toggle (``InvestorPreferences``, set from
    Settings). Default OFF — a missing prefs row or a NULL column reads as disabled — so the
    live overlay is opt-in and the persisted EOD-close valuation stays authoritative until the
    user turns it on. The overlay applies iff ``feed_entitled AND intraday_enabled``."""
    val = session.scalars(
        select(models.InvestorPreferences.intraday_enabled).where(
            models.InvestorPreferences.tenant_id == tenant_id,
            models.InvestorPreferences.portfolio_id == portfolio_id,
        )
    ).first()
    return bool(val)


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
    # An empty `quotes` map alone isn't fatal: a portfolio holding ONLY late-striking funds
    # (metron-ops#112) never gets a real per-ticker quote for them, yet the SAME artifact's
    # `fund_proxies` map can still drive a same-day estimate for every one of them. Only bail
    # here when there's neither a real quote NOR any held ticker `_overlay` could estimate.
    if not quotes and not any(t.upper() in fund_proxy.FUND_PROXY for t in tickers):
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=stale, reason="unavailable")
    if stale:
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=True, reason="stale")
    # EOD close is the baseline; the fresh intraday last overrides it per symbol (also the
    # source ``fund_eod_close`` for a late-striking-fund same-day estimate — metron-ops#112).
    from api.services import prices as price_service

    eod_closes = price_service.latest_close_by_symbol(session, tickers)
    overlay, estimated = _overlay(
        session, tickers, quotes, today=today, eod_closes=eod_closes, reader=reader, now=now
    )
    if not overlay:
        return None, IntradayMeta(applied=False, as_of_utc=as_of, stale=stale, reason="unavailable")
    merged = dict(eod_closes)
    merged.update(overlay)
    return merged, IntradayMeta(
        applied=True, as_of_utc=as_of, stale=False, n_priced=len(overlay), n_total=len(tickers),
        estimated_tickers=frozenset(estimated),
    )


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

    # Two gates, deployment axis first: the feed must be OFFERED here (reason="feed" on a
    # no-feed tier), then the user's single intraday toggle must be ON (reason="off", the
    # default). Either off → value from EOD close, and the UI shows no live-tick label.
    if not feed_entitled:
        return None, IntradayMeta(applied=False, reason="feed")
    if not intraday_enabled(session, tenant_id, portfolio_id):
        return None, IntradayMeta(applied=False, reason="off")
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
    if not intraday_enabled(session, tenant_id, portfolio_id):
        return TodaySummary(available=False, reason="off")

    held = analytics.valued_holdings(session, tenant_id, portfolio_id, account_ids=account_ids)
    base = analytics._base_currency(session, portfolio_id)
    if not held:
        return TodaySummary(available=True, base_currency=base, reason="unavailable")

    quotes, as_of, stale = load_quotes(reader=reader, now=now)
    if not quotes:
        return TodaySummary(available=False, base_currency=base, reason="unavailable", as_of_utc=as_of, stale=True)

    yf_by_ticker = _yf_symbol_by_ticker(session, [h.ticker for h in held])
    return _today_summary(held, quotes, yf_by_ticker, base, as_of=as_of, stale=stale)


def _today_summary(
    held: list,
    quotes: dict,
    yf_by_ticker: dict[str, str],
    base: str,
    *,
    as_of: datetime | None,
    stale: bool,
) -> TodaySummary:
    """Overnight·intraday·day P&L decomposition for one set of valued holdings against the
    intraday snapshot quotes. Shared by ``today_view`` (one scope) and ``today_by_account``
    (every account off ONE snapshot decode), so the per-holding math is single-sourced."""
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


def today_by_account(
    session: Session,
    tenant_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    *,
    feed_entitled: bool,
    account_ids: Collection[uuid.UUID] | None = None,
    reader=None,
    now: datetime | None = None,
) -> dict[uuid.UUID, TodaySummary]:
    """Per-account TODAY decomposition computed in ONE pass — values every account's
    holdings once (``analytics.valued_holdings_by_account``: a single price + FX lookup over
    the union of tickers) and decodes the intraday snapshot once, then decomposes per account.

    Replaces the per-account ``today_view`` N+1 in ``performance.account_period_returns``
    (was N× valued_holdings + N× snapshot decode for an N-account portfolio — the dominant
    cost of the Accounts panel). Empty/out-of-scope accounts are omitted from the result;
    callers treat an absent account_id as "no TODAY legs"."""
    from api.services import analytics

    now = now or datetime.now(UTC)
    if not feed_entitled:
        return {}
    per_acct = analytics.valued_holdings_by_account(session, tenant_id, portfolio_id)
    if account_ids is not None:
        scope = set(account_ids)
        per_acct = {aid: hs for aid, hs in per_acct.items() if aid in scope}
    if not per_acct:
        return {}
    base = analytics._base_currency(session, portfolio_id)
    quotes, as_of, stale = load_quotes(reader=reader, now=now)
    if not quotes:
        return {}
    all_tickers = [h.ticker for hs in per_acct.values() for h in hs]
    yf_by_ticker = _yf_symbol_by_ticker(session, all_tickers)
    return {
        aid: _today_summary(held, quotes, yf_by_ticker, base, as_of=as_of, stale=stale)
        for aid, held in per_acct.items()
        if held
    }
