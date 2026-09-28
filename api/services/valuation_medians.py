"""SP1500-broad sector & country median multiples from the data spine (Holdings metrics).

Reads `market_data/valuation_medians/latest.json` (produced weekly by alpha-engine-data's
metron_market_data collector over the SP1500 ∪ held universe — yfinance-derived → feed-gated).
This is the peer benchmark the Holdings "by sector → country" view bands each holding against.

Same yfinance source/units as `fundamentals.py`, so a band's median and a holding's per-row
multiple are directly comparable. `dividend_yield` is normalized percent → fraction to match
`TickerFundamentals.dividend_yield`. Metron is a pure S3 consumer: missing artifact / absent
group → omitted, never fabricated. The source is injectable for tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from api.services import spine_artifacts

VALUATION_MEDIANS_KEY = "market_data/valuation_medians/latest.json"


@dataclass
class GroupMedians:
    n: int
    trailing_pe: float | None
    forward_pe: float | None
    price_to_book: float | None
    price_to_sales: float | None
    ev_ebitda: float | None
    dividend_yield: float | None   # fraction (artifact gives a percent → normalized ÷100)


@dataclass
class ValuationMediansSnapshot:
    as_of: date | None
    by_sector: dict[str, GroupMedians]
    by_country: dict[str, GroupMedians]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    # Fail-soft (None on any read error); shared short-TTL cache — see spine_artifacts.
    return spine_artifacts.read_json(VALUATION_MEDIANS_KEY, bucket=_bucket())


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_group(d: dict) -> GroupMedians:
    div = _f(d, "dividend_yield")
    return GroupMedians(
        n=int(d.get("n") or 0),
        trailing_pe=_f(d, "trailing_pe"),
        forward_pe=_f(d, "forward_pe"),
        price_to_book=_f(d, "price_to_book"),
        price_to_sales=_f(d, "price_to_sales"),
        ev_ebitda=_f(d, "ev_ebitda"),
        dividend_yield=(div / 100.0 if div is not None else None),  # percent → fraction
    )


def _parse_groups(raw: dict | None) -> dict[str, GroupMedians]:
    out: dict[str, GroupMedians] = {}
    for name, body in (raw or {}).items():
        if isinstance(body, dict):
            out[name] = _parse_group(body)
    return out


def load_valuation_medians(*, reader=None) -> ValuationMediansSnapshot:
    """The latest valuation-medians snapshot. ``reader`` (a no-arg callable returning the raw
    artifact dict) is injectable for tests; defaults to the S3 read."""
    art = (reader or _default_reader)() or {}
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return ValuationMediansSnapshot(
        as_of=as_of,
        by_sector=_parse_groups(art.get("by_sector")),
        by_country=_parse_groups(art.get("by_country")),
    )
