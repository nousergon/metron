"use client";

// What-if reallocation sandbox (metron-ops#171) — a collapsible panel ON the Holdings
// page (not a dedicated route). The user types hypothetical per-ticker weights; the
// panel recomputes the SAME diagnostics the Holdings page already shows — concentration
// (HHI / top-N), sector exposure, weighted valuation aggregates, weighted Attractiveness
// — as current → hypothetical deltas, using the shared math in lib/whatif.ts so the
// baseline and hypothetical columns can never drift onto different formulas.
//
// NO-GENERATED-WEIGHTS INVARIANT (binding, not a style preference — pre-registration
// positioning constraint): this panel never generates a weight and never ranks one
// hypothetical over another; it carries no prescriptive copy of any kind. The user
// hypothesizes; the panel measures. Locked by tests/test_whatif_no_suggestion_strings.py
// (mirrors tests/test_no_advisor_strings.py's grep-based invariant, metron-PR217).
//
// Ephemeral: every value here is client-side React state. No persistence, no server
// call, no portfolio mutation (spec §6) — closing/reloading the panel discards it.

import { useMemo, useState } from "react";
import type { Holding } from "@/lib/api";
import { accountingPercent, decimal, multiple, pct1 } from "@/lib/format";
import {
  CASH_ROW_KEY,
  baselineWeights,
  computeConcentration,
  computeSectorExposure,
  delta,
  includedHoldings,
  normalizeToFull,
  setWeight,
  sumWeights,
  weightFnFromMap,
  weightedAttractiveness,
  weightedValuationAggregate,
  zeroAllWeights,
  type Delta,
  type WeightMap,
} from "@/lib/whatif";

/** The valuation ratios rolled up into the panel's weighted-aggregate table — the same
 *  ratios the Holdings Valuation band already shows per ticker (metron-PR215). */
const VALUATION_METRICS: { key: string; label: string; value: (h: Holding) => number | null; render: (v: number) => string }[] = [
  { key: "pe", label: "P/E", value: (h) => h.pe, render: multiple },
  { key: "fwd_pe", label: "Fwd P/E", value: (h) => h.fwd_pe, render: multiple },
  { key: "pb", label: "P/B", value: (h) => h.pb, render: multiple },
  { key: "ps", label: "P/S", value: (h) => h.ps, render: multiple },
  { key: "ev_ebitda", label: "EV/EBITDA", value: (h) => h.ev_ebitda, render: multiple },
  { key: "peg", label: "PEG", value: (h) => h.peg, render: (v) => decimal(v, 2) },
  { key: "div_yield", label: "Div yield", value: (h) => h.div_yield, render: pct1 },
];

function fmtPct(v: number | null): string {
  return v != null ? accountingPercent(v) : "—";
}

function fmtDeltaPct(d: Delta): string {
  if (d.delta == null) return "—";
  const sign = d.delta > 0 ? "+" : d.delta < 0 ? "−" : "";
  return `${sign}${Math.abs(d.delta * 100).toFixed(1)}pp`;
}

function fmtScore(v: number | null): string {
  return v != null ? decimal(v, 1) : "—";
}

function fmtScoreDelta(d: Delta): string {
  if (d.delta == null) return "—";
  const sign = d.delta > 0 ? "+" : d.delta < 0 ? "−" : "";
  return `${sign}${Math.abs(d.delta).toFixed(1)}`;
}

/** One editable weight cell — a plain % input, matching the existing inline-edit house
 *  style (TickerCell / ClassifyCellContent) of a controlled input with immediate
 *  onChange (no separate Save step; the panel-level "sum ties to 100%" line is the
 *  running confirmation, not a per-cell commit). */
function WeightInput({ value, onChange }: { value: number; onChange: (next: number) => void }) {
  return (
    <input
      type="number"
      inputMode="decimal"
      min={0}
      max={100}
      step={0.1}
      className="w-20 rounded border border-line bg-paper px-2 py-1 text-right text-sm tabular-nums"
      value={Number((value * 100).toFixed(2))}
      onChange={(e) => {
        const pct = e.target.valueAsNumber;
        onChange(Number.isFinite(pct) ? pct / 100 : 0);
      }}
      aria-label="Hypothetical weight (%)"
    />
  );
}

export function HoldingsWhatIfPanel({ holdings }: { holdings: Holding[] }) {
  // Controlled open state (not a bare <details>/<summary> pair): the panel's body
  // repeats each ticker (once per hypothetical-weight row) alongside the always-mounted
  // Holdings table above it, so — unlike the column-preset <details> disclosure, whose
  // content is unique — this body must NOT stay mounted-but-hidden while collapsed, or
  // every ticker on the page becomes ambiguous to a screen reader / find-in-page. Only
  // render the (heavier, duplicate-ticker-bearing) body once the user actually opens it.
  const [open, setOpen] = useState(false);
  const included = useMemo(() => includedHoldings(holdings), [holdings]);
  const baseline = useMemo(() => baselineWeights(holdings), [holdings]);
  const [hypothetical, setHypothetical] = useState<WeightMap>(baseline);

  // If the underlying Holdings data set changes shape (e.g. combine/account-scope
  // toggle) while the panel is open, re-seed from the new baseline rather than keep
  // stale tickers around — the panel never persists across a data reload.
  const [seedKey, setSeedKey] = useState(() => included.map((h) => h.ticker).sort().join("|"));
  const currentKey = included.map((h) => h.ticker).sort().join("|");
  if (currentKey !== seedKey) {
    setSeedKey(currentKey);
    setHypothetical(baseline);
  }

  const baseWeightFn = useMemo(() => weightFnFromMap(baseline), [baseline]);
  const hypoWeightFn = useMemo(() => weightFnFromMap(hypothetical), [hypothetical]);

  const baseConcentration = useMemo(() => computeConcentration(holdings, baseWeightFn), [holdings, baseWeightFn]);
  const hypoConcentration = useMemo(() => computeConcentration(holdings, hypoWeightFn), [holdings, hypoWeightFn]);

  const baseSectors = useMemo(() => computeSectorExposure(holdings, baseWeightFn), [holdings, baseWeightFn]);
  const hypoSectors = useMemo(() => computeSectorExposure(holdings, hypoWeightFn), [holdings, hypoWeightFn]);
  const sectorNames = useMemo(() => {
    const names = new Set<string>();
    for (const r of baseSectors) names.add(r.sector);
    for (const r of hypoSectors) names.add(r.sector);
    return [...names].sort((a, b) => {
      if (a === "Unclassified") return 1;
      if (b === "Unclassified") return -1;
      const bw = hypoSectors.find((r) => r.sector === b)?.weight ?? 0;
      const aw = hypoSectors.find((r) => r.sector === a)?.weight ?? 0;
      return bw - aw;
    });
  }, [baseSectors, hypoSectors]);

  const baseAttractiveness = useMemo(() => weightedAttractiveness(holdings, baseWeightFn), [holdings, baseWeightFn]);
  const hypoAttractiveness = useMemo(() => weightedAttractiveness(holdings, hypoWeightFn), [holdings, hypoWeightFn]);

  const cashWeight = hypothetical[CASH_ROW_KEY] ?? 0;
  const namedTotal = sumWeights(hypothetical, { includeCash: false });
  const fullTotal = namedTotal + cashWeight;

  const editWeight = (ticker: string, pct: number) => setHypothetical((prev) => setWeight(prev, ticker, pct));
  const zero = (ticker: string) => editWeight(ticker, 0);
  const zeroAll = () => setHypothetical((prev) => zeroAllWeights(prev));
  const resetToBaseline = () => setHypothetical(baseline);
  const normalize = () => setHypothetical((prev) => normalizeToFull(prev));

  return (
    <details className="rounded-lg border border-line" open={open}>
      {/* onClick (not relying on the native <details> toggle event) so the open/closed
          state is driven directly by React — the body's content is gated on it (see
          header comment), and a click-driven native toggle can otherwise race a
          controlled `open` prop. preventDefault stops the browser's own native
          open-attribute flip from fighting the controlled prop. */}
      <summary
        className="cursor-pointer list-none rounded-lg px-4 py-3 text-sm font-medium text-ink"
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
      >
        What-if: try hypothetical weights
      </summary>
      {open ? (
      <div className="space-y-6 border-t border-line px-4 py-4">
        <p className="text-xs text-muted">
          Enter hypothetical weights per ticker below. This is a sandbox measurement only — nothing here changes your
          actual holdings, and nothing is saved when you close this panel.
        </p>

        {/* ── Weight inputs ── */}
        <div>
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-4 py-2 font-medium">Ticker</th>
                  <th className="px-4 py-2 text-right font-medium">Current weight</th>
                  <th className="px-4 py-2 text-right font-medium">Hypothetical weight</th>
                  <th className="px-4 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {included.map((h) => (
                  <tr key={h.ticker} className="border-b border-line last:border-0">
                    <td className="px-4 py-2 font-medium">{h.ticker}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtPct(baseline[h.ticker] ?? 0)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">
                      <WeightInput value={hypothetical[h.ticker] ?? 0} onChange={(next) => editWeight(h.ticker, next)} />
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => zero(h.ticker)}
                        className="text-xs text-muted underline hover:text-ink"
                      >
                        Zero
                      </button>
                    </td>
                  </tr>
                ))}
                <tr className="border-b border-line bg-surface/60 last:border-0">
                  <td className="px-4 py-2 font-medium text-muted">Cash (residual)</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">—</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtPct(cashWeight)}</td>
                  <td className="px-4 py-2" />
                </tr>
              </tbody>
            </table>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className={`tabular-nums ${Math.abs(fullTotal - 1) < 1e-9 ? "text-muted" : "text-negative"}`}>
              Hypothetical weights + cash: {fmtPct(fullTotal)}
              {Math.abs(fullTotal - 1) >= 1e-9 ? " (does not tie to 100% — use Normalize)" : ""}
            </span>
            <div className="flex gap-3">
              <button type="button" onClick={normalize} className="text-muted underline hover:text-ink">
                Normalize to 100%
              </button>
              <button type="button" onClick={zeroAll} className="text-muted underline hover:text-ink">
                Zero all
              </button>
              <button type="button" onClick={resetToBaseline} className="text-muted underline hover:text-ink">
                Reset to current
              </button>
            </div>
          </div>
        </div>

        {/* ── Concentration: current vs hypothetical ── */}
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Concentration</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-4 py-2 font-medium">Metric</th>
                  <th className="px-4 py-2 text-right font-medium">Current</th>
                  <th className="px-4 py-2 text-right font-medium">Hypothetical</th>
                  <th className="px-4 py-2 text-right font-medium">Δ</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-line last:border-0">
                  <td className="px-4 py-2">HHI</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{baseConcentration.hhi.toFixed(3)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{hypoConcentration.hhi.toFixed(3)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {(hypoConcentration.hhi - baseConcentration.hhi).toFixed(3)}
                  </td>
                </tr>
                <tr className="border-b border-line last:border-0">
                  <td className="px-4 py-2">Top 5 share</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtPct(baseConcentration.top5Share)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtPct(hypoConcentration.top5Share)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {fmtDeltaPct(delta(baseConcentration.top5Share, hypoConcentration.top5Share))}
                  </td>
                </tr>
                <tr className="border-b border-line last:border-0">
                  <td className="px-4 py-2">Top 10 share</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtPct(baseConcentration.top10Share)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtPct(hypoConcentration.top10Share)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {fmtDeltaPct(delta(baseConcentration.top10Share, hypoConcentration.top10Share))}
                  </td>
                </tr>
                <tr className="last:border-0">
                  <td className="px-4 py-2">Largest position</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">
                    {fmtPct(baseConcentration.maxPositionWeight)} ({baseConcentration.maxPositionTicker ?? "—"})
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {fmtPct(hypoConcentration.maxPositionWeight)} ({hypoConcentration.maxPositionTicker ?? "—"})
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {fmtDeltaPct(delta(baseConcentration.maxPositionWeight, hypoConcentration.maxPositionWeight))}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Sector exposure: current vs hypothetical ── */}
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Sector exposure</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-4 py-2 font-medium">Sector</th>
                  <th className="px-4 py-2 text-right font-medium">Current</th>
                  <th className="px-4 py-2 text-right font-medium">Hypothetical</th>
                  <th className="px-4 py-2 text-right font-medium">Δ</th>
                </tr>
              </thead>
              <tbody>
                {sectorNames.map((sector) => {
                  const b = baseSectors.find((r) => r.sector === sector)?.weight ?? 0;
                  const hyp = hypoSectors.find((r) => r.sector === sector)?.weight ?? 0;
                  return (
                    <tr key={sector} className="border-b border-line last:border-0">
                      <td className="px-4 py-2 font-medium">{sector}</td>
                      <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtPct(b)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{fmtPct(hyp)}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{fmtDeltaPct(delta(b, hyp))}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Weighted valuation aggregates: current vs hypothetical ── */}
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Weighted valuation</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line bg-surface text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-4 py-2 font-medium">Metric</th>
                  <th className="px-4 py-2 text-right font-medium">Current</th>
                  <th className="px-4 py-2 text-right font-medium">Hypothetical</th>
                </tr>
              </thead>
              <tbody>
                {VALUATION_METRICS.map((m) => {
                  const b = weightedValuationAggregate(holdings, baseWeightFn, m.value);
                  const hyp = weightedValuationAggregate(holdings, hypoWeightFn, m.value);
                  return (
                    <tr key={m.key} className="border-b border-line last:border-0">
                      <td className="px-4 py-2">{m.label}</td>
                      <td className="px-4 py-2 text-right tabular-nums text-muted">{b != null ? m.render(b) : "—"}</td>
                      <td className="px-4 py-2 text-right tabular-nums">{hyp != null ? m.render(hyp) : "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── Weighted Attractiveness: current vs hypothetical ── */}
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Weighted Attractiveness</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-line">
            <table className="w-full text-sm">
              <tbody>
                <tr>
                  <td className="px-4 py-2">Portfolio score (0–100)</td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtScore(baseAttractiveness)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmtScore(hypoAttractiveness)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {fmtScoreDelta(delta(baseAttractiveness, hypoAttractiveness))}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[11px] text-muted">
            Uncovered tickers (outside the scanner universe) are excluded from the weighted average, same as the
            Holdings Attractiveness column — never counted as a zero score.
          </p>
        </div>
      </div>
      ) : null}
    </details>
  );
}
