// What-if reallocation sandbox — pure weight math + diagnostics recompute
// (metron-ops#171). Everything here is a MEASUREMENT of a HYPOTHETICAL weight set the
// user typed in: no generated weights, no prescriptive output of any kind, nothing
// proposed. The same functions compute the baseline (current) column and the
// hypothetical column — only the weight input differs — so the two columns can never
// silently drift onto different math (mirrors the Python concentration/sector engine
// in portfolio_analytics/domain/diagnostics.py, ported here for client-side reuse
// since the diagnostics API has no hypothetical-weight input).
//
// Ephemeral by design: everything in this module is a pure function over an in-memory
// weight map. No persistence, no server call, no portfolio mutation.

import type { Holding } from "@/lib/api";

export const CASH_ROW_KEY = "__cash__";

/** One row of editable hypothetical weight state, keyed by ticker (+ CASH_ROW_KEY for
 *  the residual). Values are fractions (0.20 = 20%), not percentages. */
export type WeightMap = Record<string, number>;

/** The baseline (current) weight of a holding — share of total INCLUDED (priced,
 *  non-cash, positive market value) market value. Mirrors the Python engine's
 *  ``_concentration`` / ``_sector_rows`` denominator exactly, so the baseline column
 *  and the diagnostics page can never silently disagree. */
export function includedHoldings(holdings: Holding[]): Holding[] {
  return holdings.filter((h) => h.security_type !== "cash" && (h.market_value ?? 0) > 0);
}

/** Baseline weights (current portfolio), keyed by ticker, summing to 1 over the
 *  included holdings. Cash / unpriced rows are excluded — same contract as the
 *  Python engine. */
export function baselineWeights(holdings: Holding[]): WeightMap {
  const included = includedHoldings(holdings);
  const total = included.reduce((acc, h) => acc + (h.market_value ?? 0), 0);
  const out: WeightMap = {};
  if (total <= 0) return out;
  for (const h of included) out[h.ticker] = (h.market_value ?? 0) / total;
  return out;
}

/** Sum of a weight map's values (excludes CASH_ROW_KEY by default — pass
 *  `includeCash: true` to sum every row, e.g. for the tie-out check). */
export function sumWeights(weights: WeightMap, opts: { includeCash?: boolean } = {}): number {
  let total = 0;
  for (const [key, w] of Object.entries(weights)) {
    if (!opts.includeCash && key === CASH_ROW_KEY) continue;
    total += w;
  }
  return total;
}

/** Apply one ticker's hypothetical weight edit. The cash row silently absorbs the
 *  residual so the panel's total always ties out to 100% (spec: "displayed
 *  hypothetical weights + cash residual sum to 100%") — the user only ever edits
 *  named-ticker rows; cash is derived, never typed. A weight is clamped to
 *  [0, 1]: zeroing a holding is explicitly supported (spec: "holdings can be
 *  zeroed"); negative weights (a short) are out of scope for this sandbox. */
export function setWeight(weights: WeightMap, ticker: string, next: number): WeightMap {
  const clamped = Math.max(0, Math.min(1, next));
  const updated: WeightMap = { ...weights, [ticker]: clamped };
  return recomputeCash(updated);
}

/** Zero a single ticker's hypothetical weight — the residual flows to cash. */
export function zeroWeight(weights: WeightMap, ticker: string): WeightMap {
  return setWeight(weights, ticker, 0);
}

/** Zero every named-ticker weight at once — the panel's "Zero all" control. All freed
 *  weight flows to cash (same residual contract as a single `zeroWeight`), leaving the
 *  full 100% sitting in cash so the user starts a from-scratch reallocation instead of
 *  zeroing tickers one at a time. */
export function zeroAllWeights(weights: WeightMap): WeightMap {
  const cleared: WeightMap = {};
  for (const ticker of Object.keys(weights)) {
    if (ticker === CASH_ROW_KEY) continue;
    cleared[ticker] = 0;
  }
  return recomputeCash(cleared);
}

/** Recompute the cash row as 1 − Σ(named-ticker weights), floored at 0. A weight set
 *  that already exceeds 100% (every non-cash weight increased) reports 0 cash rather
 *  than a negative residual — the tie-out assertion (below) is what surfaces that
 *  state to the caller instead of a silently negative cash row. */
export function recomputeCash(weights: WeightMap): WeightMap {
  const namedTotal = sumWeights(weights, { includeCash: false });
  return { ...weights, [CASH_ROW_KEY]: Math.max(0, 1 - namedTotal) };
}

/** Normalize every non-cash weight proportionally so named tickers + cash sum to
 *  exactly 100% — the panel's explicit "normalize to 100%" control (spec §1). Scales
 *  every ticker weight by the same factor; cash is recomputed as the residual (0 when
 *  the named weights already summed to ≥100%, since normalizing scales DOWN in that
 *  case). A zero-total weight set is left untouched (nothing to scale). */
export function normalizeToFull(weights: WeightMap): WeightMap {
  const namedTotal = sumWeights(weights, { includeCash: false });
  if (namedTotal <= 0) return recomputeCash(weights);
  const scale = 1 / namedTotal;
  const scaled: WeightMap = {};
  for (const [ticker, w] of Object.entries(weights)) {
    if (ticker === CASH_ROW_KEY) continue;
    scaled[ticker] = w * scale;
  }
  return recomputeCash(scaled);
}

/** Tie-out assertion (spec §7): displayed hypothetical weights + cash residual sum to
 *  100%, within floating-point tolerance. Callers use this to gate rendering /
 *  tests use it to lock the invariant. */
export function tiesOutToFull(weights: WeightMap, tolerance = 1e-9): boolean {
  return Math.abs(sumWeights(weights, { includeCash: true }) - 1) <= tolerance;
}

// ── Diagnostics recompute — same math, parameterized by a weight lookup ──────────

/** A weight lookup function: ticker → fraction of portfolio. Both the baseline column
 *  and the hypothetical column call the SAME diagnostics functions below with a
 *  different lookup — the math itself never forks. */
export type WeightFn = (ticker: string) => number;

export function weightFnFromMap(weights: WeightMap): WeightFn {
  return (ticker: string) => weights[ticker] ?? 0;
}

export type ConcentrationMetrics = {
  nPositions: number;
  hhi: number;
  effectiveN: number;
  top5Share: number;
  top10Share: number;
  maxPositionTicker: string | null;
  maxPositionWeight: number;
};

/** Herfindahl-Hirschman index + top-N share, ported from
 *  ``portfolio_analytics.domain.diagnostics._concentration`` — Σwᵢ² over per-ticker
 *  weight fractions, 1/HHI effective positions, top-5/top-10 share of included value.
 *  Positions with a zero hypothetical weight are excluded from n_positions/max-position
 *  (a zeroed holding is no longer "held"), matching how the cash row absorbs it. */
export function computeConcentration(holdings: Holding[], weightFn: WeightFn): ConcentrationMetrics {
  const rows = includedHoldings(holdings)
    .map((h) => ({ ticker: h.ticker, weight: weightFn(h.ticker) }))
    .filter((r) => r.weight > 0)
    .sort((a, b) => b.weight - a.weight);

  if (rows.length === 0) {
    return { nPositions: 0, hhi: 0, effectiveN: 0, top5Share: 0, top10Share: 0, maxPositionTicker: null, maxPositionWeight: 0 };
  }
  const hhi = rows.reduce((acc, r) => acc + r.weight * r.weight, 0);
  return {
    nPositions: rows.length,
    hhi,
    effectiveN: hhi > 0 ? 1 / hhi : 0,
    top5Share: rows.slice(0, 5).reduce((acc, r) => acc + r.weight, 0),
    top10Share: rows.slice(0, 10).reduce((acc, r) => acc + r.weight, 0),
    maxPositionTicker: rows[0].ticker,
    maxPositionWeight: rows[0].weight,
  };
}

export type SectorRow = { sector: string; weight: number };

const UNCLASSIFIED_SECTOR = "Unclassified";

/** Sector exposure breakdown, ported from ``_sector_rows`` (benchmark comparison
 *  omitted — the sandbox measures the hypothetical portfolio only, no benchmark
 *  input). Weight is `weightFn(ticker)` grouped by the holding's already-canonical
 *  `sector` field (resolved server-side; never re-derived here). Sorted heaviest
 *  first, Unclassified always last. */
export function computeSectorExposure(holdings: Holding[], weightFn: WeightFn): SectorRow[] {
  const bySector = new Map<string, number>();
  for (const h of includedHoldings(holdings)) {
    const w = weightFn(h.ticker);
    if (w <= 0) continue;
    const label = h.sector || UNCLASSIFIED_SECTOR;
    bySector.set(label, (bySector.get(label) ?? 0) + w);
  }
  return [...bySector.entries()]
    .map(([sector, weight]) => ({ sector, weight }))
    .sort((a, b) => {
      if (a.sector === UNCLASSIFIED_SECTOR) return 1;
      if (b.sector === UNCLASSIFIED_SECTOR) return -1;
      return b.weight - a.weight;
    });
}

/** Weighted-average aggregate of a single valuation ratio (P/E, Fwd P/E, P/B, P/S,
 *  EV/EBITDA, PEG, dividend yield, …) across weighted, non-null holdings — the
 *  "portfolio-level rollup" of the existing Holdings valuation columns (spec §2).
 *  Weights are renormalized over the tickers that actually carry the metric (a null
 *  ratio is a coverage gap, never treated as 0) so a coverage gap never drags the
 *  aggregate toward zero; returns null when NO weighted holding carries the metric,
 *  matching the table's own "—" null semantics rather than fabricating a value. */
export function weightedValuationAggregate(
  holdings: Holding[],
  weightFn: WeightFn,
  metric: (h: Holding) => number | null,
): number | null {
  let weightedSum = 0;
  let coveredWeight = 0;
  for (const h of includedHoldings(holdings)) {
    const w = weightFn(h.ticker);
    if (w <= 0) continue;
    const v = metric(h);
    if (v == null) continue;
    weightedSum += w * v;
    coveredWeight += w;
  }
  return coveredWeight > 0 ? weightedSum / coveredWeight : null;
}

/** Weighted Attractiveness aggregate — same coverage-renormalized weighted-average
 *  contract as `weightedValuationAggregate`, applied to `h.attractiveness`. Uncovered
 *  tickers (`attractiveness: null` — off-feed or outside the SP1500 scanner universe)
 *  are excluded from both the numerator and the weight denominator, preserving the
 *  existing Holdings N/A semantics rather than treating an uncovered ticker as a 0
 *  score. Returns null (renders "—") when NO weighted holding is covered. */
export function weightedAttractiveness(holdings: Holding[], weightFn: WeightFn): number | null {
  return weightedValuationAggregate(holdings, weightFn, (h) => h.attractiveness);
}

// ── Current → hypothetical delta ──────────────────────────────────────────────────

export type Delta = { baseline: number | null; hypothetical: number | null; delta: number | null };

/** current → hypothetical delta for one metric. `null` on either side propagates to a
 *  `null` delta (never a fabricated "0 change") — mirrors the table's own null
 *  semantics for a coverage gap. */
export function delta(baseline: number | null, hypothetical: number | null): Delta {
  return {
    baseline,
    hypothetical,
    delta: baseline != null && hypothetical != null ? hypothetical - baseline : null,
  };
}
