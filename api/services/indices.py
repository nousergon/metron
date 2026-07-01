"""Major-index intraday strip for the Overview — SPY / ONEQ / QQQ / IWM as proxies for
the S&P 500 / Nasdaq Composite / Nasdaq 100 / Russell 2000.

Reads the ``indices`` map from the alpha-engine-data **data spine**
(``market_data/intraday/latest.json``, produced every 5 min during US regular trading
hours by the metron_market_data collector — see that module's INDEX_PROXY_SYMBOLS).
Index VALUES carry a separate index license; the tradeable ETF prices the spine
publishes are ordinary equity trades, so the ETF is the proxy. The artifact is
yfinance-derived → licensed → the feature is feed-gated (Pro), locked in the no-feed
beta until the licensed feed lands.

Metron is a pure S3 consumer: a missing artifact / absent symbol → marked unavailable
WITH a reason, never fabricated. Change vs prior close is computed here from the
pass-through last/prev_close. The source is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

INTRADAY_KEY = "market_data/intraday/latest.json"

# Display order + the index each ETF proxy tracks (label shown in the strip). Both Nasdaq
# proxies are shown — ONEQ tracks the broad Nasdaq Composite (the "Nasdaq" the financial
# press headlines), QQQ the mega-cap Nasdaq-100; the two routinely diverge on breadth.
INDEX_LABELS: dict[str, str] = {
    "SPY": "S&P 500",
    "ONEQ": "Nasdaq Composite",
    "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000",
}
INDEX_ORDER: list[str] = ["SPY", "ONEQ", "QQQ", "IWM"]

# The producer writes every 5 min during the session; flag the snapshot stale once it's
# older than this (e.g. the market closed, or the demand-gated feed paused) so the UI can
# say "as of …" honestly instead of implying a live tick.
STALE_AFTER_SECONDS = 20 * 60


@dataclass
class IndexQuote:
    symbol: str
    label: str
    last: float | None
    prev_close: float | None
    open: float | None
    change: float | None       # last − prev_close (absolute), None if either is missing
    change_pct: float | None   # change / prev_close (fraction), None if prev_close missing/0 — "Today"
    session_date: str | None
    suspect: bool              # producer flagged a >40% move vs prior close (bad scrape?)
    # Period returns from cached daily closes (metron-ops#87) — enriched by the endpoint
    # (the service is a pure S3 consumer; close history lives in the DB). None until set.
    ytd_pct: float | None = None
    ltm_pct: float | None = None


@dataclass
class IndicesSnapshot:
    available: bool
    reason: str | None = None
    as_of_utc: str | None = None  # ISO8601 Z from the artifact (the producer's write time)
    stale: bool = False
    indices: list[IndexQuote] = field(default_factory=list)


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=INTRADAY_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "markets unavailable"
        logger.warning("data-spine read failed %s: %s", INTRADAY_KEY, e)
        return None


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _is_stale(as_of_utc: str | None, now: datetime) -> bool:
    """True when the snapshot is older than ``STALE_AFTER_SECONDS`` (or its timestamp is
    unparseable — better to under-promise freshness than imply a live tick)."""
    if not as_of_utc:
        return True
    try:
        beat = datetime.fromisoformat(str(as_of_utc).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now - beat).total_seconds() > STALE_AFTER_SECONDS


def proxy_day_return(symbol: str, *, reader=None, now: datetime | None = None) -> float | None:
    """The tracking-proxy ETF's same-day fractional return, read from the intraday
    artifact's ``fund_proxies`` map — the input to the mutual-fund same-day ESTIMATE
    (metron-ops#112, ``api/services/fund_proxy.py`` mechanism B). Mirrors
    ``load_indices``'s "only a quote dated for the live session carries a real today
    move" discipline exactly: a proxy quote whose ``session_date`` isn't today's NYSE
    session (pre-open / stale overnight artifact) has no today move to lend the fund, so
    this returns ``None`` rather than leak yesterday's move in as today's estimate.

    Returns ``None`` when the artifact is unavailable, the proxy symbol is absent from
    ``fund_proxies``, the quote isn't dated for today's session, or ``prev_close`` is
    missing/zero (never fabricated)."""
    from api.services import security_perf

    now = now or datetime.now(UTC)
    session_today = security_perf.market_today(now).isoformat()
    art = (reader or _default_reader)()
    if not art:
        return None
    raw = art.get("fund_proxies") or {}
    q = raw.get(symbol)
    if not isinstance(q, dict):
        return None
    session_date = q.get("session_date")
    if session_date is None or str(session_date) != session_today:
        return None
    last = _f(q, "last")
    prev = _f(q, "prev_close")
    if last is None or not prev:
        return None
    return (last - prev) / prev


def load_indices(*, reader=None, now: datetime | None = None) -> IndicesSnapshot:
    """The latest major-index intraday strip. ``reader`` (a no-arg callable returning the
    raw intraday artifact dict) and ``now`` are injectable for tests; ``reader`` defaults
    to the S3 read, ``now`` to the current UTC time."""
    from api.services import security_perf

    now = now or datetime.now(UTC)
    # The current trading session in NYSE market time — the SAME notion of "today" the
    # portfolio TODAY tile uses (security_perf.market_today). Pre-open / on a weekend or
    # holiday the overnight artifact still carries the PRIOR session's last/prev_close, so a
    # quote whose session_date != today's session has no "today" move to show: we keep its
    # level but suppress change/change_pct (metron-ops#96, mirrors the metron#119 portfolio
    # guard) rather than relabel the last completed session's move as TODAY.
    session_today = security_perf.market_today(now).isoformat()
    art = (reader or _default_reader)()
    if not art:
        return IndicesSnapshot(False, reason="Market data unavailable — the intraday feed hasn't published yet.")
    as_of_utc = art.get("as_of_utc")
    raw = art.get("indices") or {}
    quotes: list[IndexQuote] = []
    for sym in INDEX_ORDER:
        q = raw.get(sym)
        if not isinstance(q, dict):
            continue  # absent from this snapshot — omitted, not fabricated
        last = _f(q, "last")
        prev = _f(q, "prev_close")
        session_date = q.get("session_date")
        is_today = session_date is not None and str(session_date) == session_today
        # Only a quote dated for the live session carries a real "today" move; otherwise the
        # level still renders but the move is flat/blank (no stale prior-session % as TODAY).
        change = (last - prev) if (is_today and last is not None and prev is not None) else None
        change_pct = (change / prev) if (change is not None and prev) else None
        quotes.append(
            IndexQuote(
                symbol=sym,
                label=INDEX_LABELS.get(sym, sym),
                last=last,
                prev_close=prev,
                open=_f(q, "open"),
                change=change,
                change_pct=change_pct,
                session_date=session_date,
                suspect=bool(q.get("suspect", False)),
            )
        )
    if not quotes:
        return IndicesSnapshot(
            False,
            reason="No index quotes in the latest intraday snapshot yet.",
            as_of_utc=as_of_utc,
            stale=_is_stale(as_of_utc, now),
        )
    return IndicesSnapshot(True, as_of_utc=as_of_utc, stale=_is_stale(as_of_utc, now), indices=quotes)
