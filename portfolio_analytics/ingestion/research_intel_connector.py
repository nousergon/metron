"""research_intel connector — the neutral market-intelligence artifact.

Reads ``research_intel/latest.json`` (published weekly by the crucible-research run,
EPIC config#1499 Phase 0 / config#1500) from the data-spine bucket and normalizes it
into a canonical, typed intel snapshot. Mirrors ``reference_connector``'s S3-read +
fail-soft posture (a missing/unreadable/corrupt artifact yields a snapshot with
``error`` set, never a raise, so callers keep last-good rather than blanking the
surface).

**Why this is NOT a ``BrokerConnector`` / ``SNAPSHOT_SOURCES`` member** (the #117 body
sketched registering it there): that machinery governs per-ACCOUNT holdings ownership +
silver-merge dedup (see ``ingestion.base.SNAPSHOT_SOURCES`` / ``ingestion.ingest``).
research_intel carries GLOBAL market intel (one regime label, per-sector ratings,
per-ticker attractiveness) with no account, no position, and no ownership to resolve —
routing it through the holdings pipeline would be incorrect. It instead flows through
its own last-good store (``research_intel_store``) and its own read-only, entitlement-
gated API surface (``api.routers.research_intel``), gated to the paid AI Advisor tier.

Contract: ``nousergon_lib.contracts`` ``research_intel.schema.json`` v1. Evolution is
additive-only, so parsing is deliberately lenient — unknown fields are ignored, and
every field except a ticker key is optional/nullable (graceful degrade), matching the
schema's ``additionalProperties`` posture and the no-silent-fabrication rule (a value we
cannot read stays ``None``; it is never invented).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "research_intel"
RESEARCH_INTEL_KEY = "research_intel/latest.json"

_VALID_REGIMES = frozenset({"bull", "neutral", "bear"})
_VALID_RATINGS = frozenset({"overweight", "market_weight", "underweight"})


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _num(v: Any) -> float | None:
    """Coerce to float, or ``None`` for missing/unparseable (never fabricate)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


# ── canonical typed snapshot ─────────────────────────────────────────────────
@dataclass(frozen=True)
class SectorRating:
    rating: str | None
    rationale: str | None = None

    def to_dict(self) -> dict:
        return {"rating": self.rating, "rationale": self.rationale}


@dataclass(frozen=True)
class MarketBreadth:
    pct_above_50d_ma: float | None = None
    pct_above_200d_ma: float | None = None
    advance_decline_ratio: float | None = None

    def to_dict(self) -> dict:
        return {
            "pct_above_50d_ma": self.pct_above_50d_ma,
            "pct_above_200d_ma": self.pct_above_200d_ma,
            "advance_decline_ratio": self.advance_decline_ratio,
        }


@dataclass(frozen=True)
class AttractivenessBreakdown:
    quant_score: float | None = None
    qual_score: float | None = None
    factor_subscore: float | None = None
    weighted_base: float | None = None
    macro_shift: float | None = None

    def to_dict(self) -> dict:
        return {
            "quant_score": self.quant_score,
            "qual_score": self.qual_score,
            "factor_subscore": self.factor_subscore,
            "weighted_base": self.weighted_base,
            "macro_shift": self.macro_shift,
        }


@dataclass(frozen=True)
class Thesis:
    bull_case: str | None = None
    sector: str | None = None

    def to_dict(self) -> dict:
        return {"bull_case": self.bull_case, "sector": self.sector}


@dataclass(frozen=True)
class AttractivenessEntry:
    ticker: str
    score: float | None = None
    sector: str | None = None
    breakdown: AttractivenessBreakdown | None = None
    thesis: Thesis | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "score": self.score,
            "sector": self.sector,
            "breakdown": self.breakdown.to_dict() if self.breakdown else None,
            "thesis": self.thesis.to_dict() if self.thesis else None,
        }


@dataclass(frozen=True)
class ResearchIntelSnapshot:
    """Normalized, product-facing market-intel snapshot (global, read-only).

    ``error`` is set (and the record fields left empty) when the fetch failed, so the
    store keeps last-good and the API degrades to a ``stale`` marker rather than
    blanking. ``is_empty`` is true for the error/no-artifact case.
    """

    schema_version: int | None = None
    date: str | None = None
    generated_at: str | None = None
    market_regime: str | None = None
    regime_narrative: str | None = None
    sector_ratings: dict[str, SectorRating] = field(default_factory=dict)
    sector_modifiers: dict[str, float] = field(default_factory=dict)
    market_breadth: MarketBreadth = field(default_factory=MarketBreadth)
    attractiveness: dict[str, AttractivenessEntry] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.error is not None or (
            self.market_regime is None and not self.sector_ratings and not self.attractiveness
        )

    def for_tickers(self, tickers: list[str] | None) -> dict[str, AttractivenessEntry]:
        """The attractiveness map filtered to ``tickers`` (case-insensitive), or all of
        it when ``tickers`` is falsy. Unknown tickers are simply absent (no fabrication)."""
        if not tickers:
            return dict(self.attractiveness)
        wanted = {t.strip().upper() for t in tickers if t and t.strip()}
        return {k: v for k, v in self.attractiveness.items() if k.upper() in wanted}

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "date": self.date,
            "generated_at": self.generated_at,
            "market_regime": self.market_regime,
            "regime_narrative": self.regime_narrative,
            "sector_ratings": {k: v.to_dict() for k, v in self.sector_ratings.items()},
            "sector_modifiers": dict(self.sector_modifiers),
            "market_breadth": self.market_breadth.to_dict(),
            "attractiveness": {k: v.to_dict() for k, v in self.attractiveness.items()},
            "error": self.error,
        }

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> ResearchIntelSnapshot:
        """Normalize a parsed ``research_intel`` artifact (pure, lenient).

        Malformed sub-entries are dropped, not raised — one bad ticker row must not
        blank the whole snapshot. Enum-constrained fields (regime, rating) that carry
        an out-of-contract value are nulled rather than trusted."""
        regime = _str(artifact.get("market_regime"))
        if regime is not None and regime not in _VALID_REGIMES:
            logger.warning("research_intel: dropping out-of-contract regime %r", regime)
            regime = None

        sector_ratings: dict[str, SectorRating] = {}
        for sector, raw in (artifact.get("sector_ratings") or {}).items():
            if not isinstance(raw, dict):
                continue
            rating = _str(raw.get("rating"))
            if rating is not None and rating not in _VALID_RATINGS:
                rating = None
            sector_ratings[sector] = SectorRating(rating=rating, rationale=_str(raw.get("rationale")))

        sector_modifiers: dict[str, float] = {}
        for sector, raw in (artifact.get("sector_modifiers") or {}).items():
            val = _num(raw)
            if val is not None:
                sector_modifiers[sector] = val

        breadth_raw = artifact.get("market_breadth") or {}
        breadth = MarketBreadth(
            pct_above_50d_ma=_num(breadth_raw.get("pct_above_50d_ma")),
            pct_above_200d_ma=_num(breadth_raw.get("pct_above_200d_ma")),
            advance_decline_ratio=_num(breadth_raw.get("advance_decline_ratio")),
        )

        attractiveness: dict[str, AttractivenessEntry] = {}
        for ticker, raw in (artifact.get("attractiveness") or {}).items():
            if not isinstance(raw, dict):
                continue
            sym = _str(raw.get("ticker")) or (ticker if isinstance(ticker, str) and ticker.strip() else None)
            if not sym:
                continue
            bd_raw = raw.get("breakdown")
            breakdown = AttractivenessBreakdown(
                quant_score=_num(bd_raw.get("quant_score")),
                qual_score=_num(bd_raw.get("qual_score")),
                factor_subscore=_num(bd_raw.get("factor_subscore")),
                weighted_base=_num(bd_raw.get("weighted_base")),
                macro_shift=_num(bd_raw.get("macro_shift")),
            ) if isinstance(bd_raw, dict) and bd_raw else None
            th_raw = raw.get("thesis")
            thesis = Thesis(
                bull_case=_str(th_raw.get("bull_case")),
                sector=_str(th_raw.get("sector")),
            ) if isinstance(th_raw, dict) and th_raw else None
            attractiveness[sym] = AttractivenessEntry(
                ticker=sym,
                score=_num(raw.get("score")),
                sector=_str(raw.get("sector")),
                breakdown=breakdown,
                thesis=thesis,
            )

        version = artifact.get("schema_version")
        return cls(
            schema_version=version if isinstance(version, int) else None,
            date=_str(artifact.get("date")),
            generated_at=_str(artifact.get("generated_at")),
            market_regime=regime,
            regime_narrative=_str(artifact.get("regime_narrative")),
            sector_ratings=sector_ratings,
            sector_modifiers=sector_modifiers,
            market_breadth=breadth,
            attractiveness=attractiveness,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchIntelSnapshot:
        """Rehydrate a snapshot from its own ``to_dict`` form (the last-good store)."""
        snap = cls.from_artifact(data)
        # ``from_artifact`` re-normalizes; nothing extra to carry (error is never stored).
        return snap


def read_research_intel_artifact() -> dict[str, Any] | None:
    """Read + parse ``research_intel/latest.json`` from the data-spine bucket.

    Returns ``None`` on any failure (fail-soft), mirroring ``reference_connector``."""
    import boto3

    s3 = boto3.client("s3")
    bucket = _bucket()
    try:
        obj = s3.get_object(Bucket=bucket, Key=RESEARCH_INTEL_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # missing object / no creds / parse error
        logger.warning("research_intel read failed s3://%s/%s: %s", bucket, RESEARCH_INTEL_KEY, e)
        return None


class ResearchIntelConnector:
    """Fail-soft reader over the ``research_intel`` S3 artifact.

    ``reader`` is injectable for tests (returns the parsed artifact dict directly); the
    default reads from S3 via ``read_research_intel_artifact``. ``sync`` never raises —
    a transient fetch failure returns a snapshot with ``error`` set so the store keeps
    last-good, exactly like every ``BrokerConnector``."""

    source = SOURCE

    def __init__(self, reader: Callable[[], dict[str, Any] | None] | None = None):
        self._reader = reader or read_research_intel_artifact

    def sync(self) -> ResearchIntelSnapshot:
        try:
            artifact = self._reader()
        except Exception as e:  # noqa: BLE001 — degrade to last-good, never crash the refresh
            logger.warning("research_intel sync failed: %s", e)
            return ResearchIntelSnapshot(error=str(e))
        if not artifact:
            return ResearchIntelSnapshot(error="research_intel artifact unavailable")
        return ResearchIntelSnapshot.from_artifact(artifact)
