"""EOD close + FX source backed by the alpha-engine-data **data spine**.

`alpha-engine-data` is the single market-data ground truth for the Nous Ergon system.
Metron reads its EOD closes + FX from `data`'s S3 artifacts and makes no direct
market-data API calls. This module is the `PriceSource` / `HistorySource` implementation
that reads those artifacts; `prices.fetch_latest_closes` / `fetch_close_history` default
to it.

It answers BOTH the equity symbols and the FX-pair symbols the engine asks for — the FX
layer (`api.services.fx`) reuses the price-source seam with `{CCY}USD=X` pair symbols, so
a request for `HKDUSD=X` is served from the FX artifact (the rate as a `ClosePoint`).

Artifacts (written by `alpha-engine-data/collectors/metron_market_data.py`):
    market_data/eod_closes/latest.json      {closes: {yf_symbol: {close, currency, bar_date}}}
    market_data/fx/latest.json              {base, rates: {CCY: rate}, as_of}
    market_data/close_history/{yf_symbol}.json  {closes: [[date, close], …]}
    market_data/fx_history/{CCY}.json           {rates: [[date, rate], …]}

Fail-soft, mirroring the prior yfinance source: a missing artifact / unresolvable symbol
yields no point for that symbol (caller shows cost basis, never a fabricated value).
The bucket comes from ``MARKET_DATA_BUCKET`` (default ``alpha-engine-research``) — read
from the env so this engine layer stays free of the api config.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from datetime import date

from portfolio_analytics.admission import Invalid, admit_schema, read_number
from portfolio_analytics.prices.source import ClosePoint

logger = logging.getLogger(__name__)

CLOSES_LATEST_KEY = "market_data/eod_closes/latest.json"
FX_LATEST_KEY = "market_data/fx/latest.json"
CLOSE_HISTORY_PREFIX = "market_data/close_history/"
# Consolidated close-history artifact (metron-ops#233): a single file containing all
# symbols' close series + currency map, written alongside per-ticker files during the
# transition period. The consumer loads this when present to avoid N sequential reads.
CLOSE_HISTORY_CONSOLIDATED_KEY = f"{CLOSE_HISTORY_PREFIX}consolidated.json"
FX_HISTORY_PREFIX = "market_data/fx_history/"
_FX_PAIR_SUFFIX = "USD=X"  # the engine asks FX as {CCY}USD=X; the artifact base is USD

# Runtime admission (metron-ops-I373). The producer versions this reader is written for —
# the same pins as tests/contracts/metron_{closes,fx,close_history,fx_history}.schema.json.
# An artifact declaring any other (or no) version is refused whole and read as absent.
CLOSES_SCHEMA_VERSION = 1
FX_SCHEMA_VERSION = 1
CLOSE_HISTORY_SCHEMA_VERSION = 1
FX_HISTORY_SCHEMA_VERSION = 1
# The price basis every close series this engine computes returns from must share. A
# series on a different basis would be chained with dividend-adjusted ones into one NAV
# or return, so it is refused rather than mixed (metron-ops-I373).
CLOSE_HISTORY_ADJUSTMENT_BASIS = "dividend_adjusted"


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _s3():
    import boto3
    return boto3.client("s3")


def _read_json(s3, bucket: str, key: str) -> dict | None:
    """Read + parse an S3 JSON artifact; ``None`` on any failure (fail-soft)."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except Exception as e:  # missing object / no creds / parse error
        logger.warning("data-spine read failed s3://%s/%s: %s", bucket, key, e)
        return None


def _read_admitted(
    s3, bucket: str, key: str, *, expected: int, adjustment_basis: str | None = None
) -> dict | None:
    """:func:`_read_json`, then refuse anything not on the pinned contract.

    A refused artifact reads as absent (``None``) — the same fail-soft outcome as a
    missing object — and the refusal is logged at ERROR by :func:`admit_schema`, so
    an unknown producer version is never parsed as the known shape. When
    ``adjustment_basis`` is given the artifact must declare exactly that basis.
    """
    art = _read_json(s3, bucket, key)
    if admit_schema(art, expected=expected, artifact=key) is not None:
        return None
    if art is not None and adjustment_basis is not None:
        found = art.get("adjustment_basis")
        if found != adjustment_basis:
            logger.error(
                "data-spine artifact refused — %s: adjustment_basis %r is not the %r every "
                "other close series is on; mixing bases would corrupt returns",
                key, found, adjustment_basis,
            )
            return None
    return art


def _positive_finite(value) -> float | None:
    """``value`` as a finite float > 0, else ``None`` (NaN, Infinity, ≤ 0, garbage)."""
    reading = read_number(value)
    if isinstance(reading, Invalid) or reading is None or reading <= 0:
        return None
    return reading


def _fx_currency(symbol: str) -> str | None:
    """``"HKDUSD=X"`` → ``"HKD"``; non-FX-pair symbol → ``None``. USD base only (the
    artifact base); a non-USD-base pair simply isn't recognized and is treated as equity."""
    if symbol.endswith(_FX_PAIR_SUFFIX) and len(symbol) > len(_FX_PAIR_SUFFIX):
        return symbol[: -len(_FX_PAIR_SUFFIX)]
    return None


def spine_latest_closes(symbols: list[str], *, s3=None) -> dict[str, ClosePoint]:
    """Latest close per symbol from the spine. Equity symbols resolve from the
    eod_closes artifact; ``{CCY}USD=X`` pairs resolve from the fx artifact (rate as a
    ``ClosePoint``)."""
    s3 = s3 or _s3()
    bucket = _bucket()
    closes_art = _read_admitted(s3, bucket, CLOSES_LATEST_KEY, expected=CLOSES_SCHEMA_VERSION) or {}
    closes_map = closes_art.get("closes", {})
    fx_art = _read_admitted(s3, bucket, FX_LATEST_KEY, expected=FX_SCHEMA_VERSION) or {}
    fx_rates = fx_art.get("rates", {})
    fx_as_of = _parse_date(fx_art.get("as_of"))

    out: dict[str, ClosePoint] = {}
    refused: list[str] = []
    for sym in symbols:
        ccy = _fx_currency(sym)
        if ccy is not None:
            raw_rate = fx_rates.get(ccy)
            rate = _positive_finite(raw_rate)
            if rate is not None and fx_as_of is not None:
                out[sym] = ClosePoint(bar_date=fx_as_of, close=rate)
            elif raw_rate is not None:
                refused.append(sym)
            continue
        point = closes_map.get(sym)
        if not point or not isinstance(point, dict):
            continue
        bar = _parse_date(point.get("bar_date"))
        close = _positive_finite(point.get("close"))
        if bar is not None and close is not None:
            out[sym] = ClosePoint(bar_date=bar, close=close)
        else:
            refused.append(sym)
    if refused:
        # Present but not a positive finite number on a valid date: omitted (the caller
        # shows cost basis), and said so — never priced from a NaN/Infinity/zero close.
        logger.error("data-spine latest close/rate refused for %s: not a positive finite value", refused)
    return out


def _parse_series(series: list, start: date, end: date, *, label: str = "series") -> list[ClosePoint]:
    """Parse raw series rows ``[[date, val], …]``, filter to window, return sorted.

    All-or-nothing (metron-ops-I373): a row that is malformed, has an unparseable date,
    or a value that is not a positive finite number refuses the WHOLE series — it is
    returned empty and logged at ERROR — rather than silently skipping that row and
    handing back a series with an undetected hole that downstream returns would read as
    a real gap-free path. Rows outside ``[start, end]`` are filtered, not refused.
    """
    if not isinstance(series, list):
        logger.error("data-spine %s refused: rows are a %s, not a list", label, type(series).__name__)
        return []
    points: list[ClosePoint] = []
    for row in series:
        d = val = None
        if isinstance(row, (list, tuple)) and len(row) == 2:
            d, val = _parse_date(row[0]), _positive_finite(row[1])
        if d is None or val is None:
            logger.error(
                "data-spine %s refused: row %r is not [ISO date, positive finite number]",
                label, row,
            )
            return []
        if start <= d <= end:
            points.append(ClosePoint(bar_date=d, close=val))
    if points:
        points.sort(key=lambda p: p.bar_date)
    return points


def spine_close_history(symbols: list[str], start: date, end: date, *, s3=None) -> dict[str, list[ClosePoint]]:
    """Daily close series per symbol over ``[start, end]`` from the spine.

    Primary source (metron-ops#233): the consolidated artifact at
    ``market_data/close_history/consolidated.json`` (all equity symbols in one file) —
    the consumer loads it once and filters in memory. Falls back to per-symbol history
    files during the transition period. Equity → consolidated / close_history/{sym}.json;
    ``{CCY}USD=X`` → fx_history/{CCY}.json (FX history is always 1–2 currencies, not N).

    S3 reads are parallelized via ``ThreadPoolExecutor`` for the per-symbol fallback path:
    N tickers take ~1 round-trip latency instead of N sequential round-trips. The thread
    pool is kept small enough to avoid S3 rate limits for typical portfolio sizes (≤30
    symbols)."""
    if not symbols:
        return {}
    s3 = s3 or _s3()
    bucket = _bucket()
    out: dict[str, list[ClosePoint]] = {}

    # FX pairs — always read from per-currency files (few currencies, never N>1).
    for sym in symbols:
        ccy = _fx_currency(sym)
        if ccy is not None:
            key = f"{FX_HISTORY_PREFIX}{ccy}.json"
            art = _read_admitted(s3, bucket, key, expected=FX_HISTORY_SCHEMA_VERSION)
            series = (art or {}).get("rates", [])
            points = _parse_series(series, start, end, label=key)
            if points:
                out[sym] = points

    # Equity symbols — try consolidated file first, fall back to parallel per-ticker reads.
    equity_symbols = [sym for sym in symbols if _fx_currency(sym) is None]
    if not equity_symbols:
        return out
    consolidated = _read_json(s3, bucket, CLOSE_HISTORY_CONSOLIDATED_KEY)
    if consolidated is not None and (
        admit_schema(
            consolidated, expected=CLOSE_HISTORY_SCHEMA_VERSION, artifact=CLOSE_HISTORY_CONSOLIDATED_KEY
        )
        is not None
        or consolidated.get("adjustment_basis") != CLOSE_HISTORY_ADJUSTMENT_BASIS
    ):
        if isinstance(consolidated, dict) and consolidated.get("schema_version") == CLOSE_HISTORY_SCHEMA_VERSION:
            logger.error(
                "data-spine artifact refused — %s: adjustment_basis %r is not %r",
                CLOSE_HISTORY_CONSOLIDATED_KEY, consolidated.get("adjustment_basis"),
                CLOSE_HISTORY_ADJUSTMENT_BASIS,
            )
        # Refused: fall through to the per-ticker files, each admitted on its own.
        consolidated = None
    if consolidated is not None:
        series_map = consolidated.get("series", {})
        for sym in equity_symbols:
            series = series_map.get(sym)
            if not series:
                continue
            points = _parse_series(series, start, end, label=f"{CLOSE_HISTORY_CONSOLIDATED_KEY}[{sym}]")
            if points:
                out[sym] = points
    else:
        # Fall back to per-ticker files (transition period or producer lag).
        # Parallel S3 reads via thread pool (boto3 is sync but thread-safe).
        raw: dict[str, dict | None] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(equity_symbols), 30),
        ) as pool:
            future_map = {
                pool.submit(
                    _read_admitted,
                    s3,
                    bucket,
                    f"{CLOSE_HISTORY_PREFIX}{sym}.json",
                    expected=CLOSE_HISTORY_SCHEMA_VERSION,
                    adjustment_basis=CLOSE_HISTORY_ADJUSTMENT_BASIS,
                ): sym
                for sym in equity_symbols
            }
            for future in concurrent.futures.as_completed(future_map):
                sym = future_map[future]
                try:
                    raw[sym] = future.result()
                except Exception:
                    raw[sym] = None
        for sym in equity_symbols:
            art = raw.get(sym) or {}
            series = art.get("closes", [])
            points = _parse_series(series, start, end, label=f"{CLOSE_HISTORY_PREFIX}{sym}.json")
            if points:
                out[sym] = points
    return out


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
