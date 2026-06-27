"""Consensus research from the alpha-engine-data **data spine** (metron-ops#105, Phase 1).

Reads `market_data/analyst/latest.json` (produced by alpha-engine-data's metron_market_data
`collect_analyst` — yfinance recommendationKey + price targets + #analysts, Finnhub backfills
a missing rating; FREE sources only → feed-gated), keyed by yf_symbol. Metron is a pure S3
consumer: a missing artifact / absent symbol → omitted, never fabricated. The source is
injectable for tests.

Mirrors `fundamentals.py` / `technicals.py` exactly (module-level KEY, fail-soft
`_default_reader`, injectable `reader`, dataclass + `load_*`).

Forward consensus EPS/revenue *estimates* are a PAID feed — the producer does NOT emit them.
The `estimates`-shaped placeholder fields below carry that forward: they stay None until the
paid feed lands (metron-ops#107), so the scaffolded "N/A · paid feed" columns auto-populate
with NO schema/UI change.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger(__name__)

ANALYST_KEY = "market_data/analyst/latest.json"

# Reason surfaced on the paid forward-estimate columns until the paid consensus-estimates
# feed lands (metron-ops#107). Scaffolded now so the columns exist with no later schema/UI
# change — they resolve from N/A → values the moment the feed is plumbed.
PAID_ESTIMATES_REASON = "N/A · paid feed"


@dataclass
class TickerAnalyst:
    yf_symbol: str
    consensus_rating: str | None      # yfinance/Finnhub bucket: strongBuy/buy/hold/sell/strongSell
    rating_score: float | None        # signed score in [-1, +1] (strongBuy=+1 … strongSell=-1)
    mean_target: float | None         # mean analyst price target (native price units)
    median_target: float | None       # median analyst price target
    num_analysts: int | None          # # of analysts behind the rating/targets
    # ── Paid-feed scaffolding (metron-ops#107) — always None from the free artifact. ──
    # Forward consensus estimates (EPS/revenue), consensus forward P/E, PEG-from-consensus,
    # and the estimate-revision trend are a PAID feed; the producer does not emit them. They
    # render "N/A · paid feed" until that feed lands, with no schema change here.
    forward_eps: float | None = None
    forward_revenue: float | None = None
    forward_pe_consensus: float | None = None
    peg_consensus: float | None = None
    estimate_revision_trend: float | None = None

    @property
    def estimates_available(self) -> bool:
        """True once any paid forward-estimate field is populated (the paid feed has landed).
        Drives the `N/A · paid feed` → value gate downstream (metron-ops#107)."""
        return any(
            v is not None
            for v in (
                self.forward_eps,
                self.forward_revenue,
                self.forward_pe_consensus,
                self.peg_consensus,
                self.estimate_revision_trend,
            )
        )

    def target_upside(self, price: float | None) -> float | None:
        """Price-target upside vs a live price, as a fraction (mean_target/price − 1).
        None when either side is missing or the price is non-positive — never fabricated."""
        if self.mean_target is None or price in (None, 0) or price < 0:
            return None
        try:
            return self.mean_target / price - 1.0
        except (TypeError, ZeroDivisionError):
            return None


@dataclass
class AnalystSnapshot:
    as_of: date | None
    by_symbol: dict[str, TickerAnalyst]


def _bucket() -> str:
    return os.environ.get("MARKET_DATA_BUCKET", "alpha-engine-research")


def _default_reader() -> dict | None:
    import boto3

    try:
        obj = boto3.client("s3").get_object(Bucket=_bucket(), Key=ANALYST_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:  # fail-soft: the consumer degrades to "consensus unavailable"
        logger.warning("data-spine read failed %s: %s", ANALYST_KEY, e)
        return None


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(d: dict, key: str) -> int | None:
    v = d.get(key)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse(yf_symbol: str, d: dict) -> TickerAnalyst:
    rating = d.get("consensus_rating")
    return TickerAnalyst(
        yf_symbol=yf_symbol,
        consensus_rating=str(rating) if rating is not None else None,
        rating_score=_f(d, "rating_score"),
        mean_target=_f(d, "mean_target"),
        median_target=_f(d, "median_target"),
        num_analysts=_i(d, "num_analysts"),
        # Paid forward estimates (metron-ops#107) — tolerate them already if a future
        # artifact starts carrying them; absent in the free producer → None.
        forward_eps=_f(d, "forward_eps"),
        forward_revenue=_f(d, "forward_revenue"),
        forward_pe_consensus=_f(d, "forward_pe_consensus"),
        peg_consensus=_f(d, "peg_consensus"),
        estimate_revision_trend=_f(d, "estimate_revision_trend"),
    )


def load_analyst(*, reader=None) -> AnalystSnapshot:
    """The latest consensus-research snapshot, keyed by yf_symbol. ``reader`` (a no-arg
    callable returning the raw artifact dict) is injectable for tests; defaults to the S3
    read. A missing artifact yields an empty snapshot (fail-soft)."""
    art = (reader or _default_reader)() or {}
    by_symbol: dict[str, TickerAnalyst] = {}
    for sym, body in (art.get("analyst") or {}).items():
        if isinstance(body, dict):
            by_symbol[sym] = _parse(sym, body)
    as_of = None
    raw_as_of = art.get("as_of")
    if raw_as_of:
        try:
            as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            as_of = None
    return AnalystSnapshot(as_of=as_of, by_symbol=by_symbol)
