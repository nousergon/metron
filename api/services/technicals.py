"""Technical indicators from the alpha-engine-data **data spine** (Holdings metrics).

Reads `market_data/technicals/latest.json` (produced daily by alpha-engine-data's
metron_market_data collector from the close_history it already publishes — yfinance-derived
→ feed-gated), keyed by yf_symbol. Metron is a pure S3 consumer: a missing artifact / absent
symbol → omitted, never fabricated. The source is injectable for tests.

Mirrors `fundamentals.py` exactly (module-level KEY, fail-soft `_default_reader`, injectable
`reader`, dataclass + `load_*`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date

from api.services import spine_artifacts
from portfolio_analytics.admission import Invalid, admit_schema, read_number

logger = logging.getLogger(__name__)

TECHNICALS_KEY = "market_data/technicals/latest.json"
# The producer version this reader is written for — the same pin as
# tests/contracts/technicals.schema.json. Any other version is refused at
# runtime (metron-ops-I373); re-pin both together when the producer bumps it.
TECHNICALS_SCHEMA_VERSION = 4


@dataclass
class TickerTechnicals:
    yf_symbol: str
    rsi_14: float | None          # Wilder RSI(14), 0-100
    macd_hist: float | None       # MACD line − signal line (price units)
    ma_50: float | None
    ma_200: float | None
    pct_to_ma_50: float | None    # last/50d-MA − 1 (fraction)
    pct_to_ma_200: float | None   # last/200d-MA − 1 (fraction)
    high_52w: float | None
    low_52w: float | None
    pct_in_52w_range: float | None  # (last − low)/(high − low), 0-1
    pct_from_52wk_high: float | None  # last/high_52w − 1 (fraction)
    mom_20d: float | None         # 20-session return (fraction)
    mom_60d: float | None         # 60-session return (fraction)


@dataclass
class TechnicalsSnapshot:
    as_of: date | None
    by_symbol: dict[str, TickerTechnicals]
    # Lineage and admission (metron-ops-I373). `schema_version`/`source` are what the
    # artifact declared. `rejected_reason` is set when the whole artifact was refused
    # (unknown/missing schema_version) — `by_symbol` is then empty, exactly as for an
    # absent artifact, but the reason is explicit. `invalid_fields` names, per symbol,
    # each field that was PRESENT but not a finite number (NaN/Infinity/garbage); those
    # fields read as None, and are distinct from fields the producer simply omitted.
    schema_version: int | None = None
    source: str | None = None
    rejected_reason: str | None = None
    invalid_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    # Fail-soft (None on any read error); shared short-TTL cache — see spine_artifacts.
    return spine_artifacts.read_json(TECHNICALS_KEY, bucket=_bucket())


def _f(d: dict, key: str, invalid: list[str]) -> float | None:
    """A finite float, or None. A present-but-invalid value (NaN, Infinity, a
    non-numeric string) also reads as None, and its key is appended to ``invalid``
    so the snapshot can say so (metron-ops-I373)."""
    reading = read_number(d.get(key))
    if isinstance(reading, Invalid):
        invalid.append(key)
        return None
    return reading


def _parse(yf_symbol: str, d: dict, invalid: list[str]) -> TickerTechnicals:
    return TickerTechnicals(
        yf_symbol=yf_symbol,
        rsi_14=_f(d, "rsi_14", invalid),
        macd_hist=_f(d, "macd_hist", invalid),
        ma_50=_f(d, "ma_50", invalid),
        ma_200=_f(d, "ma_200", invalid),
        pct_to_ma_50=_f(d, "pct_to_ma_50", invalid),
        pct_to_ma_200=_f(d, "pct_to_ma_200", invalid),
        high_52w=_f(d, "high_52w", invalid),
        low_52w=_f(d, "low_52w", invalid),
        pct_in_52w_range=_f(d, "pct_in_52w_range", invalid),
        pct_from_52wk_high=_f(d, "pct_from_52wk_high", invalid),
        mom_20d=_f(d, "mom_20d", invalid),
        mom_60d=_f(d, "mom_60d", invalid),
    )


def load_technicals(*, reader=None) -> TechnicalsSnapshot:
    """The latest technicals snapshot, keyed by yf_symbol. ``reader`` (a no-arg callable
    returning the raw artifact dict) is injectable for tests; defaults to the S3 read."""
    raw = (reader or _default_reader)()
    rejected = admit_schema(raw, expected=TECHNICALS_SCHEMA_VERSION, artifact=TECHNICALS_KEY)
    if rejected is not None:
        return TechnicalsSnapshot(
            as_of=None,
            by_symbol={},
            schema_version=raw.get("schema_version") if isinstance(raw, dict) else None,
            rejected_reason=rejected,
        )
    art = raw or {}
    by_symbol: dict[str, TickerTechnicals] = {}
    invalid_fields: dict[str, tuple[str, ...]] = {}
    for sym, body in (art.get("technicals") or {}).items():
        if isinstance(body, dict):
            invalid: list[str] = []
            by_symbol[sym] = _parse(sym, body, invalid)
            if invalid:
                invalid_fields[sym] = tuple(invalid)
    if invalid_fields:
        logger.warning(
            "%s: %d symbol(s) carried non-finite or non-numeric values, read as unavailable: %s",
            TECHNICALS_KEY, len(invalid_fields), sorted(invalid_fields),
        )
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return TechnicalsSnapshot(
        as_of=as_of,
        by_symbol=by_symbol,
        schema_version=art.get("schema_version"),
        source=art.get("source"),
        invalid_fields=invalid_fields,
    )
