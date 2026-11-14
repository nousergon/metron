"""EOD close sourcing — the price-source seam.

The engine is price-source-agnostic: ``fetch_latest_closes`` / ``fetch_close_history``
take an injectable ``source`` (used by tests and any alternate feed). The DEFAULT is the
**data spine** — Metron reads EOD closes + FX from `alpha-engine-data`'s S3 artifacts and
makes no direct market-data API calls (see ``spine_source``). The default is imported
lazily so importing this module never requires boto3 / network.

Fail-soft by symbol: a symbol the source can't resolve is simply omitted; the caller
treats an absent symbol as "no price" and shows cost basis — never a fabricated value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class InputSource:
    """Which stored producer object a close or rate was read from (metron-ops-I373).

    ``artifact`` is the object's location (``s3://bucket/key``). ``version_id`` is the
    store's immutable version of it and ``etag`` its content digest, when the store
    reports them; together they name the exact bytes the value came from, so a valuation
    built on it can be traced back after the producer overwrites the key.
    ``adjustment_basis`` is the price basis the artifact declared (``None`` for FX and for
    an artifact that declares none)."""

    artifact: str
    version_id: str | None = None
    etag: str | None = None
    adjustment_basis: str | None = None

    @property
    def ref(self) -> str:
        """One string naming the object version: the version id when the store keeps
        versions, else the content digest, else the bare location."""
        if self.version_id:
            return f"{self.artifact}?versionId={self.version_id}"
        if self.etag:
            return f"{self.artifact}?etag={self.etag}"
        return self.artifact


@dataclass(frozen=True)
class ClosePoint:
    """One symbol's most recent daily close and the session it printed.

    ``source`` is the lineage of the value: the producer object it was read from, or
    ``None`` when the source cannot say (an injected test source, a value read back from
    the cache). It is excluded from equality: two points with the same date and close are
    the same observation whichever object version carried it."""

    bar_date: date
    close: float
    source: InputSource | None = field(default=None, compare=False)


# A source maps a list of symbols to their latest closes. The default is the data spine;
# tests (and any alternate feed) inject their own.
PriceSource = Callable[[list[str]], dict[str, ClosePoint]]
# A history source maps symbols + a date range to each symbol's daily close series.
HistorySource = Callable[[list[str], date, date], dict[str, list[ClosePoint]]]


def fetch_latest_closes(symbols: Iterable[str], *, source: PriceSource | None = None) -> dict[str, ClosePoint]:
    """Latest available daily close per symbol. Deduped, order-insensitive.

    Returns ``{}`` for an empty input. Symbols the source can't price are omitted."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique:
        return {}
    if source is None:
        from portfolio_analytics.prices.spine_source import spine_latest_closes
        source = spine_latest_closes
    return source(unique)


def fetch_close_history(
    symbols: Iterable[str], start: date, end: date, *, source: HistorySource | None = None
) -> dict[str, list[ClosePoint]]:
    """Daily close series per symbol over ``[start, end]`` (inclusive). Deduped.

    Returns ``{}`` for empty input. Each symbol maps to its closes sorted ascending;
    symbols the source can't resolve are omitted (caller carries forward / skips)."""
    unique = [s for s in dict.fromkeys(symbols) if s]
    if not unique or start > end:
        return {}
    if source is None:
        from portfolio_analytics.prices.spine_source import spine_close_history
        source = spine_close_history
    return source(unique, start, end)
