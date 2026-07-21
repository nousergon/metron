// What-if reallocation sandbox math (metron-ops#171): weight normalization (zeroing,
// cash residual, normalize-to-100%) and the current → hypothetical delta computation.
// Concentration/sector/valuation-aggregate recompute reuses the SAME functions for the
// baseline and hypothetical columns (parameterized by a WeightFn) — these tests lock
// that the math is correct for both inputs, not just plausible for one.

import { describe, expect, it } from "vitest";
import type { Holding } from "@/lib/api";
import {
  CASH_ROW_KEY,
  baselineWeights,
  computeConcentration,
  computeSectorExposure,
  delta,
  normalizeToFull,
  recomputeCash,
  setWeight,
  sumWeights,
  tiesOutToFull,
  weightFnFromMap,
  weightedAttractiveness,
  weightedValuationAggregate,
  zeroAllWeights,
  zeroWeight,
} from "@/lib/whatif";

const h = (ticker: string, over: Partial<Holding> = {}): Holding =>
  ({
    ticker,
    quantity: 10,
    avg_cost: 100,
    cost_basis: 1000,
    currency: "USD",
    fx_rate: 1,
    last_price: 100,
    last_price_date: "2026-07-01",
    market_value_local: 1000,
    cost_basis_base: 1000,
    market_value: 1000,
    unrealized_gain: 0,
    unrealized_pct: 0,
    security_type: "equity",
    sector: "Technology",
    country: "United States",
    attractiveness: null,
    ...over,
  }) as Holding;

describe("baselineWeights", () => {
  it("weights sum to 1 over included (priced, non-cash, positive MV) holdings", () => {
    const holdings = [h("A", { market_value: 600 }), h("B", { market_value: 400 })];
    const w = baselineWeights(holdings);
    expect(w.A).toBeCloseTo(0.6);
    expect(w.B).toBeCloseTo(0.4);
    expect(sumWeights(w)).toBeCloseTo(1);
  });

  it("excludes cash security_type and non-positive market value rows", () => {
    const holdings = [
      h("A", { market_value: 500 }),
      h("CASHX", { market_value: 500, security_type: "cash" }),
      h("ZERO", { market_value: 0 }),
      h("NEG", { market_value: -10 }),
    ];
    const w = baselineWeights(holdings);
    expect(w.A).toBeCloseTo(1);
    expect(w.CASHX).toBeUndefined();
    expect(w.ZERO).toBeUndefined();
    expect(w.NEG).toBeUndefined();
  });

  it("returns an empty map for a zero-total portfolio (no fabricated weights)", () => {
    expect(baselineWeights([])).toEqual({});
  });
});

describe("setWeight / zeroWeight — cash residual", () => {
  it("zeroing a holding sends its weight to the cash row, not to the other tickers", () => {
    const base = { A: 0.5, B: 0.5, [CASH_ROW_KEY]: 0 };
    const next = zeroWeight(base, "A");
    expect(next.A).toBe(0);
    expect(next.B).toBe(0.5); // untouched
    expect(next[CASH_ROW_KEY]).toBeCloseTo(0.5); // absorbs A's former weight
    expect(tiesOutToFull(next)).toBe(true);
  });

  it("raising one ticker's weight shrinks the cash residual, never another ticker's weight", () => {
    const base = recomputeCash({ A: 0.3, B: 0.3 }); // cash = 0.4
    const next = setWeight(base, "A", 0.5);
    expect(next.A).toBe(0.5);
    expect(next.B).toBe(0.3); // untouched
    expect(next[CASH_ROW_KEY]).toBeCloseTo(0.2);
    expect(tiesOutToFull(next)).toBe(true);
  });

  it("clamps a weight to [0, 1]", () => {
    const next = setWeight({}, "A", 1.5);
    expect(next.A).toBe(1);
    const neg = setWeight({}, "A", -0.2);
    expect(neg.A).toBe(0);
  });

  it("floors cash at 0 when named weights already exceed 100% (never a negative residual)", () => {
    const next = recomputeCash({ A: 0.7, B: 0.6 });
    expect(next[CASH_ROW_KEY]).toBe(0);
    // The over-100% state is surfaced via tiesOutToFull, not a negative cash row.
    expect(tiesOutToFull(next)).toBe(false);
  });
});

describe("zeroAllWeights", () => {
  it("zeros every named ticker and sends the full 100% to cash", () => {
    const base = recomputeCash({ A: 0.5, B: 0.3, C: 0.2 });
    const cleared = zeroAllWeights(base);
    expect(cleared.A).toBe(0);
    expect(cleared.B).toBe(0);
    expect(cleared.C).toBe(0);
    expect(cleared[CASH_ROW_KEY]).toBeCloseTo(1);
    expect(tiesOutToFull(cleared)).toBe(true);
  });

  it("is a no-op on an already-zeroed weight set", () => {
    const zero = recomputeCash({ A: 0, B: 0 });
    const cleared = zeroAllWeights(zero);
    expect(cleared.A).toBe(0);
    expect(cleared.B).toBe(0);
    expect(cleared[CASH_ROW_KEY]).toBe(1);
  });
});

describe("normalizeToFull", () => {
  it("scales every ticker weight proportionally so weights + cash sum to 100%", () => {
    const over = recomputeCash({ A: 0.3, B: 0.3 }); // named 0.6, cash 0.4 — already ties out
    const scaledUp = { ...over, A: 0.9, B: 0.9, [CASH_ROW_KEY]: 0 }; // named 1.8, no cash
    const normalized = normalizeToFull(scaledUp);
    expect(normalized.A).toBeCloseTo(0.5); // 0.9 / 1.8
    expect(normalized.B).toBeCloseTo(0.5);
    expect(tiesOutToFull(normalized)).toBe(true);
  });

  it("normalizing an under-allocated set scales weights UP and cash absorbs nothing new", () => {
    const under = recomputeCash({ A: 0.2, B: 0.2 }); // named 0.4, cash 0.6
    const normalized = normalizeToFull(recomputeCash({ A: 0.2, B: 0.2 }));
    expect(normalized.A).toBeCloseTo(0.5); // 0.2 / 0.4
    expect(normalized.B).toBeCloseTo(0.5);
    expect(normalized[CASH_ROW_KEY]).toBeCloseTo(0);
    expect(tiesOutToFull(normalized)).toBe(true);
    expect(under[CASH_ROW_KEY]).toBeCloseTo(0.6); // sanity: pre-normalize state unchanged by the second call
  });

  it("leaves an all-zero weight set untouched (nothing to scale)", () => {
    const zero = recomputeCash({ A: 0, B: 0 });
    const normalized = normalizeToFull(zero);
    expect(normalized.A).toBe(0);
    expect(normalized.B).toBe(0);
    expect(normalized[CASH_ROW_KEY]).toBe(1);
  });
});

describe("tiesOutToFull", () => {
  it("is true for baseline weights recomputed through recomputeCash", () => {
    const w = recomputeCash(baselineWeights([h("A", { market_value: 700 }), h("B", { market_value: 300 })]));
    expect(tiesOutToFull(w)).toBe(true);
  });
});

describe("computeConcentration — same function, baseline vs hypothetical weight input", () => {
  const holdings = [
    h("A", { market_value: 250 }),
    h("B", { market_value: 250 }),
    h("C", { market_value: 250 }),
    h("D", { market_value: 250 }),
  ];

  it("baseline: 4 equal positions → HHI 0.25, effective N 4, top5/top10 share 1.0", () => {
    const base = weightFnFromMap(baselineWeights(holdings));
    const c = computeConcentration(holdings, base);
    expect(c.hhi).toBeCloseTo(0.25);
    expect(c.effectiveN).toBeCloseTo(4);
    expect(c.top5Share).toBeCloseTo(1);
    expect(c.nPositions).toBe(4);
  });

  it("hypothetical: zeroing one position drops it from n_positions; the residual goes to cash, not a proportional redistribution across the rest", () => {
    const hypo = zeroWeight(recomputeCash(baselineWeights(holdings)), "A");
    const c = computeConcentration(holdings, weightFnFromMap(hypo));
    expect(c.nPositions).toBe(3);
    // B/C/D stay at their original 0.25 each (unrenormalized) — the freed weight sits in
    // cash until the user explicitly hits Normalize, so HHI = 3 × 0.25², not 3 × (1/3)².
    expect(c.hhi).toBeCloseTo(3 * 0.25 ** 2, 5);
    expect(c.maxPositionTicker).not.toBeNull();
    expect(c.maxPositionWeight).toBeCloseTo(0.25);
  });

  it("hypothetical: concentrating everything into one ticker → HHI 1.0, effective N 1", () => {
    let w = recomputeCash(baselineWeights(holdings));
    w = setWeight(w, "A", 1);
    w = setWeight(w, "B", 0);
    w = setWeight(w, "C", 0);
    w = setWeight(w, "D", 0);
    const c = computeConcentration(holdings, weightFnFromMap(w));
    expect(c.hhi).toBeCloseTo(1);
    expect(c.effectiveN).toBeCloseTo(1);
    expect(c.maxPositionTicker).toBe("A");
  });
});

describe("computeSectorExposure", () => {
  it("groups included holdings by sector and weights sum to the same total as the weight map", () => {
    const holdings = [
      h("A", { market_value: 600, sector: "Technology" }),
      h("B", { market_value: 400, sector: "Energy" }),
    ];
    const w = weightFnFromMap(baselineWeights(holdings));
    const rows = computeSectorExposure(holdings, w);
    expect(rows.find((r) => r.sector === "Technology")?.weight).toBeCloseTo(0.6);
    expect(rows.find((r) => r.sector === "Energy")?.weight).toBeCloseTo(0.4);
  });

  it("falls back to an explicit Unclassified bucket, sorted last, never a guessed sector", () => {
    const holdings = [
      h("A", { market_value: 500, sector: "Technology" }),
      h("B", { market_value: 500, sector: null }),
    ];
    const w = weightFnFromMap(baselineWeights(holdings));
    const rows = computeSectorExposure(holdings, w);
    expect(rows[rows.length - 1].sector).toBe("Unclassified");
    expect(rows.find((r) => r.sector === "Unclassified")?.weight).toBeCloseTo(0.5);
  });

  it("hypothetical reallocation shifts sector weight without mutating the baseline call", () => {
    const holdings = [
      h("A", { market_value: 500, sector: "Technology" }),
      h("B", { market_value: 500, sector: "Energy" }),
    ];
    const base = baselineWeights(holdings);
    const hypo = setWeight(setWeight(recomputeCash(base), "A", 0.9), "B", 0.1);
    const baseRows = computeSectorExposure(holdings, weightFnFromMap(base));
    const hypoRows = computeSectorExposure(holdings, weightFnFromMap(hypo));
    expect(baseRows.find((r) => r.sector === "Technology")?.weight).toBeCloseTo(0.5);
    expect(hypoRows.find((r) => r.sector === "Technology")?.weight).toBeCloseTo(0.9);
  });
});

describe("weightedValuationAggregate", () => {
  it("computes a weighted average, renormalized over covered (non-null) holdings", () => {
    const holdings = [
      h("A", { market_value: 500, pe: 10 }),
      h("B", { market_value: 500, pe: 30 }),
    ];
    const w = weightFnFromMap(baselineWeights(holdings));
    const agg = weightedValuationAggregate(holdings, w, (x) => x.pe);
    expect(agg).toBeCloseTo(20); // equal weights, equal coverage
  });

  it("excludes a null (coverage-gap) ticker from BOTH the numerator and the weight denominator", () => {
    const holdings = [
      h("A", { market_value: 500, pe: 10 }),
      h("B", { market_value: 500, pe: null }),
    ];
    const w = weightFnFromMap(baselineWeights(holdings));
    const agg = weightedValuationAggregate(holdings, w, (x) => x.pe);
    // If the null ticker's weight were NOT excluded from the denominator, this would read 5.
    expect(agg).toBeCloseTo(10);
  });

  it("returns null when no weighted holding carries the metric — never fabricates 0", () => {
    const holdings = [h("A", { market_value: 500, pe: null })];
    const w = weightFnFromMap(baselineWeights(holdings));
    expect(weightedValuationAggregate(holdings, w, (x) => x.pe)).toBeNull();
  });
});

describe("weightedAttractiveness — respects existing N/A semantics for uncovered tickers", () => {
  it("excludes attractiveness: null tickers, matching the Holdings column's N/A treatment", () => {
    const holdings = [
      h("A", { market_value: 500, attractiveness: 80 }),
      h("UNCOVERED", { market_value: 500, attractiveness: null }),
    ];
    const w = weightFnFromMap(baselineWeights(holdings));
    expect(weightedAttractiveness(holdings, w)).toBeCloseTo(80); // not 40 — uncovered isn't a 0
  });

  it("returns null when every weighted holding is uncovered", () => {
    const holdings = [h("A", { market_value: 500, attractiveness: null })];
    const w = weightFnFromMap(baselineWeights(holdings));
    expect(weightedAttractiveness(holdings, w)).toBeNull();
  });

  it("a hypothetical reweight toward the covered ticker raises the aggregate", () => {
    const holdings = [
      h("A", { market_value: 500, attractiveness: 90 }),
      h("UNCOVERED", { market_value: 500, attractiveness: null }),
    ];
    const base = baselineWeights(holdings);
    const hypo = setWeight(recomputeCash(base), "A", 1);
    const baseScore = weightedAttractiveness(holdings, weightFnFromMap(base));
    const hypoScore = weightedAttractiveness(holdings, weightFnFromMap(hypo));
    expect(hypoScore).toBeCloseTo(90);
    expect(hypoScore).toBe(baseScore); // both fully resolve to A's score once weighted-covered
  });
});

describe("delta — current → hypothetical", () => {
  it("computes hypothetical minus baseline", () => {
    expect(delta(0.2, 0.35).delta).toBeCloseTo(0.15);
  });

  it("propagates null on either side rather than fabricating a 0 change", () => {
    expect(delta(null, 0.5).delta).toBeNull();
    expect(delta(0.5, null).delta).toBeNull();
    expect(delta(null, null).delta).toBeNull();
  });
});
