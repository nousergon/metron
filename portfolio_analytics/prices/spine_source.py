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

import json
import logging
import os
from datetime import date

from portfolio_analytics.prices.source import ClosePoint

logger = logging.getLogger(__name__)

CLOSES_LATEST_KEY = "market_data/eod_closes/latest.json"
FX_LATEST_KEY = "market_data/fx/latest.json"
CLOSE_HISTORY_PREFIX = "market_data/close_history/"
FX_HISTORY_PREFIX = "market_data/fx_history/"
_FX_PAIR_SUFFIX = "USD=X"  # the engine asks FX as {CCY}USD=X; the artifact base is USD


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
    closes_art = _read_json(s3, bucket, CLOSES_LATEST_KEY) or {}
    closes_map = closes_art.get("closes", {})
    fx_art = _read_json(s3, bucket, FX_LATEST_KEY) or {}
    fx_rates = fx_art.get("rates", {})
    fx_as_of = _parse_date(fx_art.get("as_of"))

    out: dict[str, ClosePoint] = {}
    for sym in symbols:
        ccy = _fx_currency(sym)
        if ccy is not None:
            rate = fx_rates.get(ccy)
            if rate is not None and float(rate) > 0 and fx_as_of is not None:
                out[sym] = ClosePoint(bar_date=fx_as_of, close=float(rate))
            continue
        point = closes_map.get(sym)
        if not point:
            continue
        bar = _parse_date(point.get("bar_date"))
        close = point.get("close")
        if bar is not None and close is not None and float(close) > 0:
            out[sym] = ClosePoint(bar_date=bar, close=float(close))
    return out


def spine_close_history(symbols: list[str], start: date, end: date, *, s3=None) -> dict[str, list[ClosePoint]]:
    """Daily close series per symbol over ``[start, end]`` from the per-symbol history
    artifacts. Equity → close_history/{sym}.json; ``{CCY}USD=X`` → fx_history/{CCY}.json."""
    s3 = s3 or _s3()
    bucket = _bucket()
    out: dict[str, list[ClosePoint]] = {}
    for sym in symbols:
        ccy = _fx_currency(sym)
        if ccy is not None:
            art = _read_json(s3, bucket, f"{FX_HISTORY_PREFIX}{ccy}.json")
            series = (art or {}).get("rates", [])
        else:
            art = _read_json(s3, bucket, f"{CLOSE_HISTORY_PREFIX}{sym}.json")
            series = (art or {}).get("closes", [])
        points: list[ClosePoint] = []
        for row in series:
            try:
                d, val = _parse_date(row[0]), float(row[1])
            except (TypeError, ValueError, IndexError):
                continue
            if d is not None and val > 0 and start <= d <= end:
                points.append(ClosePoint(bar_date=d, close=val))
        if points:
            out[sym] = sorted(points, key=lambda p: p.bar_date)
    return out


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
