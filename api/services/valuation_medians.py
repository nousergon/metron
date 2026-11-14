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

import logging
import os
from dataclasses import dataclass, field
from datetime import date

from api.services import spine_artifacts
from api.services.spine_artifacts import ArtifactReader, JsonObject
from portfolio_analytics.admission import admit_schema, finite_field
from portfolio_analytics.prices.source import InputSource

logger = logging.getLogger(__name__)

VALUATION_MEDIANS_KEY = "market_data/valuation_medians/latest.json"
# Pinned producer version (tests/contracts/metron_valuation_medians.schema.json). Any other
# version is refused whole at runtime (metron-ops-I373).
VALUATION_MEDIANS_SCHEMA_VERSION = 1


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
    # Set when the artifact was refused whole (off-contract schema_version). `invalid_fields`
    # names, per group (`by_sector.<name>` / `by_country.<name>`), the fields that were present
    # but not a finite number and therefore read as unavailable.
    rejected_reason: str | None = None
    invalid_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The stored producer object this snapshot was read from: its `s3://` location and
    # the VersionId / ETag S3 reported on the read (metron-ops-I373). Set for a refused
    # artifact too. `None` when the read failed or the reader was injected.
    input_source: InputSource | None = None


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_read() -> tuple[JsonObject | None, InputSource | None]:
    # Fail-soft ((None, None) on any read error); shared short-TTL cache, and the object
    # version the bytes came from — see spine_artifacts.
    return spine_artifacts.read_artifact(VALUATION_MEDIANS_KEY, bucket=_bucket())


def _parse_group(d: dict, invalid: list[str]) -> GroupMedians:
    def _f(d: dict, key: str) -> float | None:
        return finite_field(d, key, invalid)

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


def _parse_groups(
    raw: dict | None, prefix: str, invalid_fields: dict[str, tuple[str, ...]]
) -> dict[str, GroupMedians]:
    out: dict[str, GroupMedians] = {}
    for name, body in (raw or {}).items():
        if isinstance(body, dict):
            invalid: list[str] = []
            out[name] = _parse_group(body, invalid)
            if invalid:
                invalid_fields[f"{prefix}.{name}"] = tuple(invalid)
    return out


def load_valuation_medians(*, reader: ArtifactReader | None = None) -> ValuationMediansSnapshot:
    """The latest valuation-medians snapshot. ``reader`` (a no-arg callable returning the raw
    artifact dict) is injectable for tests; defaults to the S3 read."""
    if reader is not None:
        raw, input_source = reader(), None
    else:
        raw, input_source = _default_read()
    rejected = admit_schema(
        raw, expected=VALUATION_MEDIANS_SCHEMA_VERSION, artifact=VALUATION_MEDIANS_KEY
    )
    if rejected is not None:
        return ValuationMediansSnapshot(
            as_of=None, by_sector={}, by_country={}, rejected_reason=rejected, input_source=input_source
        )
    art = raw or {}
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    invalid_fields: dict[str, tuple[str, ...]] = {}
    by_sector = _parse_groups(art.get("by_sector"), "by_sector", invalid_fields)
    by_country = _parse_groups(art.get("by_country"), "by_country", invalid_fields)
    if invalid_fields:
        logger.warning(
            "%s: %d group(s) carried non-finite or non-numeric values, read as unavailable: %s",
            VALUATION_MEDIANS_KEY, len(invalid_fields), sorted(invalid_fields),
        )
    return ValuationMediansSnapshot(
        as_of=as_of,
        by_sector=by_sector,
        by_country=by_country,
        invalid_fields=invalid_fields,
        input_source=input_source,
    )
