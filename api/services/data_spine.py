"""The metron ↔ alpha-engine-data contract (the "data spine").

`alpha-engine-data` is the single market-data ground truth for the whole Nous Ergon
system — every product reads prices / FX / news from its S3 artifacts and makes NO
direct market-data API calls. This module owns Metron's side of that contract.

Today it PUBLISHES the held-ticker universe (which instruments + currencies Metron
holds) so `data` knows what EOD closes + FX rates to pull — succeeding robodashboard's
retired `robodashboard/holdings_universe.json` (nousergon/metron-ops#119). The CONSUMER
side (reading `data`'s `market_data/eod_closes` + `market_data/fx` artifacts into
`price_bars` / `fx_rates`) lands in the cutover PR; the symmetry lives here so the
schema versions stay paired.

The payload also carries a symbols-only ``tickers`` list (the deduped broker symbols)
so `nousergon-data`'s weekday ``RunDailyNews`` union step can re-source Brian's held
tickers here — the exact ``{"tickers": [...]}`` slice the retired robodashboard producer
fed it (config#1506). ``holdings``/``currencies`` (yf-priced instruments) and ``tickers``
(news-universe symbols) are two views of the SAME held set, published together on the
same nightly ``daily-refresh`` (metron-refresh.timer, 21:30 UTC among the post-close
window) so they never drift.

S3 access uses the ambient AWS credentials (the deploy box's instance role). The
publish is wired into `daily-refresh` as a best-effort step — a failure WARNs and never
costs the price/NAV refresh that has already committed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from api.db import models
from api.services import analytics

logger = logging.getLogger(__name__)

# Bump when the published shape changes; `data`'s consumer pins on it.
# v2 (config#1506): added the symbols-only ``tickers`` list — the deduped broker symbols
# nousergon-data's daily-news union re-sources here (robodashboard successor). Purely
# additive: the existing ``holdings``/``currencies`` fields are unchanged, so the
# market-data producer that pins v1 keeps working.
HOLDINGS_UNIVERSE_SCHEMA_VERSION = 2
# S3 key under the shared bucket. `data` reads this to assemble its pull universe.
HOLDINGS_UNIVERSE_KEY = "metron/holdings_universe.json"


class DataSpineUnavailable(RuntimeError):
    """Raised when the data-spine S3 round-trip can't complete (boto3/creds/bucket/key)."""


# UI-activity heartbeat — the intraday producer's demand gate. While Metron is being
# actively used, authenticated portfolio requests touch this key (throttled); the
# `alpha-engine-data` intraday producer fetches quotes ONLY while it is fresh
# (collectors/metron_market_data.py::metron_app_active), so a closed app costs zero
# upstream quote fetches. Key lives under metron/ like the holdings universe.
UI_HEARTBEAT_KEY = "metron/ui_heartbeat.json"
UI_HEARTBEAT_SCHEMA_VERSION = 1
# Throttle: at most one S3 write per interval. Must stay comfortably below the
# producer's HEARTBEAT_FRESH_SECONDS (600s) so an active session never reads stale.
_HEARTBEAT_MIN_INTERVAL_S = 120.0
_last_heartbeat_monotonic: float = 0.0


def touch_ui_heartbeat(
    *,
    session: Session | None = None,
    tenant_id: uuid.UUID | None = None,
    portfolio_id: uuid.UUID | None = None,
    s3_client=None,
    now: datetime | None = None,
) -> bool:
    """Record that the app is actively in use (throttled; fail-soft; flag-gated).

    Returns True when a heartbeat was written this call, False when throttled,
    disabled (``market_data_sync_enabled`` off), the active portfolio has the intraday
    overlay turned OFF, or the write failed. STRICTLY best-effort by design: this is
    secondary observability hung off the request path — a failure is WARN-logged (the
    recording surface) and must never break a page render; the only consequence is the
    intraday feed staying paused.

    When ``session`` + ``portfolio_id`` are passed, the heartbeat (which keeps the
    upstream intraday producer fetching) is gated on that portfolio's single intraday
    toggle — so a closed OR intraday-disabled app costs zero upstream quote fetches. The
    pref lookup runs AFTER the throttle gate, so it happens at most once per interval, not
    once per request. No context (legacy callers / tests) → not gated on the pref.
    """
    global _last_heartbeat_monotonic
    if not settings.market_data_sync_enabled:
        return False
    mono = time.monotonic()
    if mono - _last_heartbeat_monotonic < _HEARTBEAT_MIN_INTERVAL_S:
        return False
    if session is not None and portfolio_id is not None:
        from api.services import intraday  # local import: avoid a module-load cycle
        if not intraday.intraday_enabled(session, tenant_id, portfolio_id):
            return False
    ts = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _write_s3_json(
            settings.market_data_bucket, UI_HEARTBEAT_KEY,
            {"schema_version": UI_HEARTBEAT_SCHEMA_VERSION, "ts": ts}, s3_client=s3_client,
        )
    except DataSpineUnavailable as e:
        logger.warning("UI heartbeat write failed (intraday feed stays paused): %s", e)
        return False
    _last_heartbeat_monotonic = mono
    return True


def _write_s3_json(bucket: str, key: str, obj: dict, s3_client=None) -> None:
    """Write ``obj`` as compact JSON to ``s3://bucket/key``. Fail-loud — the caller
    decides whether the failure is fatal (the daily-refresh wrapper treats it as
    best-effort). ``s3_client`` is injectable for tests."""
    if s3_client is None:
        try:
            import boto3
        except ImportError as e:  # pragma: no cover - boto3 is a declared dep in prod
            raise DataSpineUnavailable("boto3 is not installed — needed for the data-spine sync.") from e
        s3_client = boto3.client("s3")
    body = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    except Exception as e:  # noqa: BLE001 - surface the underlying S3/boto error verbatim
        raise DataSpineUnavailable(f"Could not write s3://{bucket}/{key}: {e}") from e


def _securities_by_symbol(session: Session, symbols: list[str]) -> dict[str, models.Security]:
    """Held symbol → its global Security row (first by id per symbol — stable). Carries
    ``yf_symbol`` (foreign listings → exchange-suffixed) + native ``currency``."""
    if not symbols:
        return {}
    rows = session.scalars(
        select(models.Security)
        .where(models.Security.symbol.in_(symbols))
        .order_by(models.Security.symbol, models.Security.id)
    ).all()
    out: dict[str, models.Security] = {}
    for row in rows:
        out.setdefault(row.symbol, row)
    return out


def build_holdings_universe(session: Session, *, today: date | None = None) -> dict:
    """Assemble the held-ticker universe across EVERY portfolio in the DB.

    Returns the publishable payload: each held instrument under the symbol `data` should
    price it by (``yf_symbol``, foreign listings exchange-suffixed) with its native
    currency, plus the distinct non-USD currencies held (so `data` knows which FX pairs
    to fetch), plus a symbols-only ``tickers`` list (the broker symbols) that
    nousergon-data's daily-news union consumes (config#1506). Deterministic + deduped —
    a ticker held in multiple portfolios/accounts appears once."""
    today = today or date.today()
    by_yf: dict[str, str] = {}  # yf_symbol → native currency
    broker_symbols: set[str] = set()  # deduped broker symbols → the news-universe `tickers`
    skipped_unlisted: set[str] = set()
    for p in session.scalars(select(models.Portfolio)).all():
        held = analytics.holdings(session, p.tenant_id, p.id)
        symbols = [h.ticker for h in held if h.ticker]
        secs = _securities_by_symbol(session, symbols)
        for h in held:
            sec = secs.get(h.ticker)
            if sec is None:
                continue
            if sec.yf_unlisted:
                # No public listing to price (e.g. a 401(k) plan-level CIT) — the
                # broker snapshot is the price authority, so publishing it would only
                # make the data spine's yfinance pull fail every run (config#1029). Such
                # instruments also have no tradable news feed, so they're excluded from
                # the news-universe `tickers` for the same reason.
                skipped_unlisted.add(sec.symbol)
                continue
            yf = sec.yf_symbol or sec.symbol
            by_yf.setdefault(yf, sec.currency or h.currency or "USD")
            # News universe uses the plain BROKER symbol (e.g. `1299`), not the
            # exchange-suffixed yf_symbol (`1299.HK`) — news APIs key off the bare
            # ticker, matching what the retired robodashboard producer published.
            if sec.symbol:
                broker_symbols.add(sec.symbol.strip().upper())
    if skipped_unlisted:
        logger.info(
            "holdings universe: %d unlisted instrument(s) excluded (broker-snapshot-priced): %s",
            len(skipped_unlisted), ", ".join(sorted(skipped_unlisted)),
        )
    holdings = [{"yf_symbol": yf, "currency": ccy} for yf, ccy in sorted(by_yf.items())]
    currencies = sorted({ccy for ccy in by_yf.values() if ccy and ccy != "USD"})
    tickers = sorted(broker_symbols)
    return {
        "schema_version": HOLDINGS_UNIVERSE_SCHEMA_VERSION,
        "as_of": today.isoformat(),
        "source": "metron",
        "holdings": holdings,
        "currencies": currencies,
        "tickers": tickers,
    }


def publish_holdings_universe(
    session: Session, *, s3_client=None, today: date | None = None, bucket: str | None = None
) -> dict:
    """Publish the held-ticker universe to S3 for `alpha-engine-data` to consume.
    Returns the published payload. Raises ``DataSpineUnavailable`` on S3 failure."""
    payload = build_holdings_universe(session, today=today)
    _write_s3_json(bucket or settings.market_data_bucket, HOLDINGS_UNIVERSE_KEY, payload, s3_client=s3_client)
    logger.info(
        "published holdings universe: %d instruments, %d non-USD currencies, "
        "%d news tickers → s3://%s/%s",
        len(payload["holdings"]), len(payload["currencies"]), len(payload["tickers"]),
        bucket or settings.market_data_bucket, HOLDINGS_UNIVERSE_KEY,
    )
    return payload


# `alpha-engine-data`'s per-ticker Holdings-metrics collectors (fundamentals / technicals /
# analyst / sentiment — Holdings metrics) fetch ONLY over the held-ticker universe above, so a
# watchlist-only ticker (tracked but never bought — metron-ops#42/#121) got no metrics at all
# on the Holdings/watchlist comparison table (metron-ops#132 — Brian's MU report). This second
# artifact mirrors the same shape so the collectors can union it in without touching their own
# schema; a symbol already in the held universe is naturally deduped downstream (a set union).
WATCHLIST_UNIVERSE_SCHEMA_VERSION = 1
WATCHLIST_UNIVERSE_KEY = "metron/watchlist_universe.json"


def build_watchlist_universe(session: Session, *, today: date | None = None) -> dict:
    """Assemble the watchlist-only-ticker universe across EVERY portfolio in the DB — the
    SAME publishable shape as ``build_holdings_universe`` (``holdings``/``currencies``/
    ``tickers``), so `alpha-engine-data` can union the two without a schema branch.
    Deterministic + deduped — a ticker watchlisted in multiple portfolios appears once, and a
    ticker that's ALSO held is naturally re-deduped by the union on the consumer side."""
    today = today or date.today()
    by_yf: dict[str, str] = {}  # yf_symbol → native currency
    broker_symbols: set[str] = set()
    symbols: set[str] = set()
    for row in session.scalars(select(models.WatchlistItem)).all():
        sym = (row.symbol or "").strip().upper()
        if sym:
            symbols.add(sym)
    secs = _securities_by_symbol(session, sorted(symbols))
    for sym in symbols:
        sec = secs.get(sym)
        if sec is None or sec.yf_unlisted:
            continue  # no cached Security yet, or no public listing to price — never fabricated
        yf = sec.yf_symbol or sec.symbol
        by_yf.setdefault(yf, sec.currency or "USD")
        if sec.symbol:
            broker_symbols.add(sec.symbol.strip().upper())
    holdings = [{"yf_symbol": yf, "currency": ccy} for yf, ccy in sorted(by_yf.items())]
    currencies = sorted({ccy for ccy in by_yf.values() if ccy and ccy != "USD"})
    tickers = sorted(broker_symbols)
    return {
        "schema_version": WATCHLIST_UNIVERSE_SCHEMA_VERSION,
        "as_of": today.isoformat(),
        "source": "metron",
        "holdings": holdings,
        "currencies": currencies,
        "tickers": tickers,
    }


def publish_watchlist_universe(
    session: Session, *, s3_client=None, today: date | None = None, bucket: str | None = None
) -> dict:
    """Publish the watchlist-only-ticker universe to S3 for `alpha-engine-data` to consume.
    Returns the published payload. Raises ``DataSpineUnavailable`` on S3 failure."""
    payload = build_watchlist_universe(session, today=today)
    _write_s3_json(bucket or settings.market_data_bucket, WATCHLIST_UNIVERSE_KEY, payload, s3_client=s3_client)
    logger.info(
        "published watchlist universe: %d instruments, %d non-USD currencies, "
        "%d tickers → s3://%s/%s",
        len(payload["holdings"]), len(payload["currencies"]), len(payload["tickers"]),
        bucket or settings.market_data_bucket, WATCHLIST_UNIVERSE_KEY,
    )
    return payload


# Crypto wallet addresses → S3 for `nousergon-data`'s crypto-balances producer to consume
# (metron-ops#111). Metron publishes the deduped (chain, address) set; the producer queries
# the chain for balances and writes `crypto/holdings.json` back. Metron makes NO chain calls.
WALLET_ADDRESSES_SCHEMA_VERSION = 1
WALLET_ADDRESSES_KEY = "metron/crypto/wallet_addresses.json"
# The producer's output that the crypto page reads back (balances + USD value per address).
CRYPTO_HOLDINGS_KEY = "crypto/holdings.json"


def build_wallet_addresses(session: Session) -> dict:
    """The deduped (chain, address) set across ALL tracked wallets — the producer's fetch
    universe. System-internal (the producer just needs to know WHAT to query); labels and
    tenant scoping stay Metron-side, so this carries no per-tenant data. Sorted for a stable,
    diff-friendly artifact."""
    pairs = sorted(
        {(c, a) for c, a in session.execute(
            select(models.WalletAddress.chain, models.WalletAddress.address).distinct()
        ).all()}
    )
    return {
        "schema_version": WALLET_ADDRESSES_SCHEMA_VERSION,
        "addresses": [{"chain": c, "address": a} for c, a in pairs],
    }


def publish_wallet_addresses(session: Session, *, s3_client=None, bucket: str | None = None) -> dict:
    """Publish the wallet-address fetch universe to S3 for the crypto-balances producer.
    Returns the published payload. Raises ``DataSpineUnavailable`` on S3 failure — callers on
    the request path treat it as best-effort (WARN, never break the add/delete)."""
    payload = build_wallet_addresses(session)
    _write_s3_json(bucket or settings.market_data_bucket, WALLET_ADDRESSES_KEY, payload, s3_client=s3_client)
    logger.info(
        "published wallet addresses: %d (chain, address) → s3://%s/%s",
        len(payload["addresses"]), bucket or settings.market_data_bucket, WALLET_ADDRESSES_KEY,
    )
    return payload
