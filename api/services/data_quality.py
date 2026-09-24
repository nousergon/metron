"""Ingestion data-quality gates wired into the real ingestion chokepoints — layer 5 of
the dashboard-accuracy verification framework (metron-ops#219, EPIC metron-ops#210).

The gate *logic* is pure and lives engine-side in
``portfolio_analytics.ingestion.quality``; this module is the DB-aware glue that runs
it where data actually enters Metron:

  * ``persistence.persist_snapshot`` — the one bridge every broker/file import lands
    through (IBKR Flex, SnapTrade, CSV, OFX, the demo seeds) → ``gate_snapshot``
    (schema contract over the whole bronze→silver boundary).
  * ``prices.refresh_latest_prices`` / ``prices.backfill_prices`` — the only two
    writers of ``price_bars`` → ``gate_latest_closes`` / ``gate_close_history``
    (price outlier + adjusted-close continuity against recorded splits).
  * the nightly ``daily-refresh`` job, right after its price refresh →
    ``gate_stale_prices`` (a held security's cached close lagging the NYSE calendar).

**Flag mode only.** Every entry point here returns findings and logs them; none of
them returns data, and every call site passes its inputs read-only and carries on
with exactly the rows it had. A bug inside a gate is swallowed (logged) rather than
raised, so the gates can never block or fail an ingest either.

**Where findings surface.** (1) A WARNING log line per finding with the grep-stable
prefix ``[data-quality:<gate>]`` — the same channel and shape layer 2's runtime
invariants use (``[invariant:<label>]``, ``api.services.invariants``). WARNING, not
ERROR, on purpose: ERROR routes through flow-doctor to Telegram, and a flag-mode
gate with un-ratified thresholds must not page. (2) The ``data_quality`` block of
``GET /meta/status`` (layer 6's status surface), recomputed from ``price_bars`` by
``status_summary`` so it is durable across processes and restarts. (3) An import's
own contract findings on its ``ImportOut`` response, so the person who uploaded a
file sees what in it looked wrong.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, timedelta

from krepis.trading_calendar import TradingCalendarPrecedesCoverageError, TradingCalendarRangeError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import models
from api.services.security_perf import STALE_AFTER_SESSIONS, market_today, sessions_behind
from portfolio_analytics.ingestion import quality
from portfolio_analytics.ingestion.base import ConnectorSnapshot
from portfolio_analytics.ingestion.quality import Finding
from portfolio_analytics.prices.source import ClosePoint

logger = logging.getLogger(__name__)

# A held security's cached close is stale once it lags the latest NYSE session by this
# many sessions. Deliberately the SAME constant the Holdings view's ⚠ badge and the
# broker-staleness detector use (``security_perf.STALE_AFTER_SESSIONS`` = 2: 0 = priced
# today, 1 = the prior session, which is normal before today's close prints). A second,
# independently-tuned threshold would let the screen and this gate disagree — the exact
# shape of metron-ops#260.
STALE_PRICE_AFTER_SESSIONS = STALE_AFTER_SESSIONS

# Calendar-day window ``status_summary`` re-scans for price outliers. Two weeks ≈ 10
# sessions: long enough that last week's split or bad print is still visible on
# /status after a weekend, short enough that the status page never reads price_bars
# history wholesale (the table is the fleet's dominant egress amplifier, metron-ops-I279).
STATUS_OUTLIER_LOOKBACK_DAYS = 14

# One process logs a given finding (gate, subject, date) once. ``backfill_prices`` is
# called three times per portfolio per nightly refresh over the whole history, so an
# old outlier would otherwise print a dozen identical lines a night. The cap bounds
# memory for a long-lived API worker; on overflow the memo is simply cleared (worst
# case: a finding is logged a second time).
_REPORTED_CAP = 10_000
_reported: set[tuple[str, str, date | None, str]] = set()


def _report(findings: list[Finding], *, where: str) -> list[Finding]:
    """Log each finding once per process at WARNING. Returns ``findings`` unchanged."""
    for f in findings:
        key = (f.gate, f.subject, f.as_of, f.detail)
        if key in _reported:
            continue
        if len(_reported) >= _REPORTED_CAP:
            _reported.clear()
        _reported.add(key)
        logger.warning("%s (at %s)", f.render(), where)
    return findings


def _guarded(where: str, fn: Callable[[], list[Finding]]) -> list[Finding]:
    """Run a gate; a gate that raises is a gate bug, never an ingest failure. Logged at
    WARNING with the traceback (ERROR would page through flow-doctor)."""
    try:
        return _report(fn(), where=where)
    except Exception:  # noqa: BLE001 — flag mode: a broken gate must not block ingestion
        logger.warning("[data-quality] gate at %s raised; ingestion continues unaffected", where, exc_info=True)
        return []


def _splits_by_security(session: Session, security_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[tuple[date, float]]]:
    """Every recorded SPLIT (``quantity`` = new:old ratio) per security, any tenant —
    price bars are global reference data, so any tenant's recorded split is evidence
    about the shared series. Only dates and ratios leave this function."""
    ids = list(set(security_ids))
    if not ids:
        return {}
    rows = session.execute(
        select(models.Transaction.security_id, models.Transaction.trade_date, models.Transaction.quantity).where(
            models.Transaction.txn_type == "SPLIT", models.Transaction.security_id.in_(ids)
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[date, float]]] = {}
    for security_id, trade_date, ratio in rows:
        out.setdefault(security_id, []).append((trade_date, float(ratio)))
    return out


# ── schema contract ─────────────────────────────────────────────────────────────
def gate_snapshot(snapshot: ConnectorSnapshot, *, today: date | None = None) -> list[Finding]:
    """Schema contract over one connector snapshot, before anything is persisted."""
    return _guarded(
        f"persist_snapshot[{snapshot.source}]",
        lambda: quality.check_snapshot_contract(snapshot, today=today or market_today()),
    )


# ── price outlier / adjusted-close continuity ───────────────────────────────────
def gate_latest_closes(
    session: Session, incoming: list[tuple[models.Security, ClosePoint]], *, today: date | None = None
) -> list[Finding]:
    """Judge each incoming latest close against that security's previous cached close
    (the last bar strictly before it), with recorded splits as the explanation set."""

    def run() -> list[Finding]:
        splits = _splits_by_security(session, (sec.id for sec, _ in incoming))
        found: list[Finding] = []
        for sec, point in incoming:
            prior = session.execute(
                select(models.PriceBar.bar_date, models.PriceBar.close)
                .where(models.PriceBar.security_id == sec.id, models.PriceBar.bar_date < point.bar_date)
                .order_by(models.PriceBar.bar_date.desc())
                .limit(1)
            ).first()
            prior_point = ClosePoint(bar_date=prior[0], close=float(prior[1])) if prior else None
            found.extend(
                quality.check_price_series(
                    sec.symbol, [point], splits.get(sec.id, ()), prior=prior_point, today=today or market_today()
                )
            )
        return found

    return _guarded("refresh_latest_prices", run)


def gate_close_history(
    session: Session,
    series_by_security: dict[uuid.UUID, tuple[str, list[ClosePoint]]],
    *,
    start: date,
    today: date | None = None,
) -> list[Finding]:
    """Judge each incoming close series internally and at its seam with the last
    cached close before ``start`` (one windowed query for all securities)."""

    def run() -> list[Finding]:
        ids = list(series_by_security)
        if not ids:
            return []
        rn = func.row_number().over(
            partition_by=models.PriceBar.security_id, order_by=models.PriceBar.bar_date.desc()
        ).label("rn")
        ranked = (
            select(models.PriceBar.security_id, models.PriceBar.bar_date, models.PriceBar.close, rn)
            .where(models.PriceBar.security_id.in_(ids), models.PriceBar.bar_date < start)
            .subquery()
        )
        priors = {
            sid: ClosePoint(bar_date=d, close=float(c))
            for sid, d, c in session.execute(
                select(ranked.c.security_id, ranked.c.bar_date, ranked.c.close).where(ranked.c.rn == 1)
            ).all()
        }
        splits = _splits_by_security(session, ids)
        found: list[Finding] = []
        for sid, (symbol, series) in series_by_security.items():
            found.extend(
                quality.check_price_series(
                    symbol, series, splits.get(sid, ()), prior=priors.get(sid), today=today or market_today()
                )
            )
        return found

    return _guarded("backfill_prices", run)


# ── stale price ─────────────────────────────────────────────────────────────────
def stale_findings(last_bar_by_symbol: dict[str, date], *, today: date) -> list[Finding]:
    """Pure core of the stale gate: one finding per symbol whose latest cached close
    lags ``today`` by ``STALE_PRICE_AFTER_SESSIONS`` or more NYSE sessions."""
    out: list[Finding] = []
    for symbol, bar_date in sorted(last_bar_by_symbol.items()):
        try:
            behind: int | None = sessions_behind(bar_date, today)
        except TradingCalendarPrecedesCoverageError:
            behind = None  # older than the NYSE calendar itself — stale by any measure
        except TradingCalendarRangeError:
            continue  # ``today`` past the calendar's coverage: cannot judge, so don't guess
        if behind is None or behind >= STALE_PRICE_AFTER_SESSIONS:
            lag = "older than the NYSE calendar's coverage" if behind is None else f"{behind} NYSE sessions behind"
            out.append(
                Finding(
                    quality.GATE_STALE_PRICE,
                    symbol,
                    f"latest cached close is {bar_date} — {lag} as of {today}",
                    observed=None if behind is None else float(behind),
                    threshold=float(STALE_PRICE_AFTER_SESSIONS),
                    as_of=bar_date,
                )
            )
    return out


def gate_stale_prices(
    session: Session,
    symbols: list[str],
    *,
    currency_by_symbol: dict[str, str] | None = None,
    today: date | None = None,
) -> list[Finding]:
    """Stale-price flags for a portfolio's held symbols, run after the nightly price
    refresh. A held symbol with NO cached close at all is not flagged here: that is an
    unpriceable instrument (a 401(k) CIT, a cash sweep) already shown at cost basis,
    not a feed that stopped updating."""
    from api.services import prices as price_service

    def run() -> list[Finding]:
        closes = price_service.latest_close_by_symbol(session, symbols, currency_by_symbol=currency_by_symbol)
        return stale_findings({s: p.bar_date for s, p in closes.items()}, today=today or market_today())

    return _guarded("daily-refresh", run)


# ── the /status surface ─────────────────────────────────────────────────────────
def status_summary(session: Session, *, today: date | None = None) -> dict:
    """The ``data_quality`` block of ``GET /meta/status``: counts only — never symbols,
    since /status is system-wide and a symbol list would disclose tenants' holdings.

    Recomputed from ``price_bars`` on each call (bounded: one grouped max-date query
    over broker-held securities, plus ``STATUS_OUTLIER_LOOKBACK_DAYS`` of bars), so it
    reflects what is persisted right now in every process, not what one worker saw.
    Schema-contract findings concern rows as they arrived and are not re-derivable
    from persisted state; they are reported in the logs and on the import response."""
    today = today or market_today()
    held_ids = select(models.Position.security_id).distinct()
    latest = session.execute(
        select(models.Security.symbol, models.Security.currency, func.max(models.PriceBar.bar_date))
        .join(models.PriceBar, models.PriceBar.security_id == models.Security.id)
        .where(models.Security.id.in_(held_ids))
        .group_by(models.Security.id, models.Security.symbol, models.Security.currency)
    ).all()
    stale = stale_findings({f"{sym}:{ccy}": d for sym, ccy, d in latest}, today=today)

    since = today - timedelta(days=STATUS_OUTLIER_LOOKBACK_DAYS)
    rows = session.execute(
        select(models.PriceBar.security_id, models.Security.symbol, models.PriceBar.bar_date, models.PriceBar.close)
        .join(models.Security, models.Security.id == models.PriceBar.security_id)
        .where(models.PriceBar.bar_date >= since)
        .order_by(models.PriceBar.security_id, models.PriceBar.bar_date)
    ).all()
    series: dict[uuid.UUID, tuple[str, list[ClosePoint]]] = {}
    for sid, symbol, bar_date, close in rows:
        series.setdefault(sid, (symbol, []))[1].append(ClosePoint(bar_date=bar_date, close=float(close)))
    splits = _splits_by_security(session, series)
    moves: Counter[str] = Counter()
    for sid, (symbol, points) in series.items():
        for f in quality.check_price_series(symbol, points, splits.get(sid, ()), today=today):
            moves[f.gate] += 1

    return {
        "available": True,
        "mode": "flag",  # gates record findings; they never drop, block or alter data
        "as_of": today.isoformat(),
        "held_securities_priced": len(latest),
        "stale_prices": len(stale),
        "price_outliers": moves[quality.GATE_PRICE_OUTLIER],
        "split_discontinuities": moves[quality.GATE_SPLIT_DISCONTINUITY],
        "invalid_prices": moves[quality.GATE_CONTRACT],
        "outlier_window_days": STATUS_OUTLIER_LOOKBACK_DAYS,
        "thresholds": {
            "price_outlier_max_move": quality.PRICE_OUTLIER_MAX_MOVE,
            "split_ratio_match_tolerance": quality.SPLIT_RATIO_MATCH_TOLERANCE,
            "stale_after_sessions": STALE_PRICE_AFTER_SESSIONS,
        },
        "note": (
            "Ingestion data-quality gates (metron-ops#219) run in flag mode. Each finding is "
            "logged at ingestion as '[data-quality:<gate>]'; schema-contract findings are also "
            "returned on the import response."
        ),
    }
