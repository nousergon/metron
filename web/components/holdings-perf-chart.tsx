"use client";

// Holdings performance chart (metron-ops#78, #87): one indexed line per checked account,
// with a time-range selector, toggleable benchmark overlays (SPY/QQQ/IWM), and a hover
// tooltip listing each line's return at the cursor, sorted high→low. Dependency-free inline
// SVG, matching nav-chart.tsx. The server returns each series as a cumulative growth index
// (g=1.0 at its first point); this component re-ranges and re-bases to 100 client-side, so
// the range buttons are instant (no refetch).
//
// Benchmark overlays are feed-gated (Pro): in the no-feed beta `benchmarksAvailable` is
// false and only the account lines render.

import { useRef, useState } from "react";
import type { AccountSeries, BenchmarkSeries, SeriesPoint } from "@/lib/api";
import { isoDate } from "@/lib/format";

const RANGES: ReadonlyArray<readonly [string, number | null]> = [
  ["1M", 30],
  ["3M", 90],
  ["6M", 180],
  ["1Y", 365],
  ["All", null],
];

// Distinct hues for account lines; benchmarks render muted + dashed so they read as
// reference, not portfolio.
const ACCOUNT_COLORS = ["#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ec4899", "#14b8a6", "#ef4444", "#0ea5e9"];
const BENCH_COLOR = "#9ca3af";

const VIEW_W = 640;
const VIEW_H = 200;
const PAD = 10;

type Pt = { when: string; v: number };
type Line = { key: string; label: string; color: string; dashed: boolean; points: Pt[] };

/** today − `days` as an ISO date (YYYY-MM-DD), or null for the All range. */
function cutoffISO(days: number | null, now = new Date()): string | null {
  if (days == null) return null;
  const d = new Date(now);
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

/** Filter a growth series to the window and re-base to 100 at its first in-window point.
 *  Fewer than 2 in-window points → no line. Exported for testing the math in isolation. */
export function rebase(points: SeriesPoint[], cutoff: string | null): Pt[] {
  const win = cutoff ? points.filter((p) => p.when >= cutoff) : points.slice();
  if (win.length < 2) return [];
  const base = win[0]!.g;
  if (!(base > 0)) return [];
  return win.map((p) => ({ when: p.when, v: (p.g / base) * 100 }));
}

function epoch(iso: string): number {
  return Date.parse(iso);
}

/** The point in a rebased series nearest a target epoch (used for the hover readout). */
export function valueAt(points: Pt[], targetEpoch: number): Pt | null {
  let best: Pt | null = null;
  let bestD = Infinity;
  for (const p of points) {
    const d = Math.abs(epoch(p.when) - targetEpoch);
    if (d < bestD) {
      bestD = d;
      best = p;
    }
  }
  return best;
}

type HoverRow = { key: string; label: string; color: string; pct: number; when: string };

/** Each line's return (rebased value − 100) at the cursor epoch, sorted high→low. The
 *  re-based index is 100 at the window start, so `v − 100` is the % return over the visible
 *  range up to that point. Exported for testing the ordering + math. */
export function hoverRows(lines: Line[], targetEpoch: number): HoverRow[] {
  const rows: HoverRow[] = [];
  for (const l of lines) {
    const pt = valueAt(l.points, targetEpoch);
    if (pt) rows.push({ key: l.key, label: l.label, color: l.color, pct: pt.v - 100, when: pt.when });
  }
  return rows.sort((a, b) => b.pct - a.pct);
}

function fmtPct(pct: number): string {
  return `${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%`;
}

export function HoldingsPerfChart({
  accounts,
  benchmarks,
  benchmarksAvailable,
}: {
  accounts: AccountSeries[];
  benchmarks: BenchmarkSeries[];
  benchmarksAvailable: boolean;
}) {
  const [rangeIdx, setRangeIdx] = useState(RANGES.length - 1); // default All
  const [hidden, setHidden] = useState<Set<string>>(new Set()); // toggled-off benchmark symbols
  const [hoverE, setHoverE] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const cutoff = cutoffISO(RANGES[rangeIdx]![1]);

  const lines: Line[] = [];
  accounts.forEach((a, i) => {
    const pts = rebase(a.points, cutoff);
    if (pts.length >= 2) {
      lines.push({ key: `acct:${a.account_id}`, label: a.name, color: ACCOUNT_COLORS[i % ACCOUNT_COLORS.length]!, dashed: false, points: pts });
    }
  });
  for (const b of benchmarks) {
    if (hidden.has(b.symbol)) continue;
    const pts = rebase(b.points, cutoff);
    if (pts.length >= 2) {
      lines.push({ key: `bench:${b.symbol}`, label: b.symbol, color: BENCH_COLOR, dashed: true, points: pts });
    }
  }

  // Shared domain across every visible line (all are indexed to 100 → one comparable scale).
  const allEpochs = lines.flatMap((l) => l.points.map((p) => epoch(p.when)));
  const allVals = lines.flatMap((l) => l.points.map((p) => p.v));
  const t0 = allEpochs.length ? Math.min(...allEpochs) : 0;
  const t1 = allEpochs.length ? Math.max(...allEpochs) : 1;
  const vmin = allVals.length ? Math.min(...allVals) : 100;
  const vmax = allVals.length ? Math.max(...allVals) : 100;
  const innerW = VIEW_W - 2 * PAD;
  const innerH = VIEW_H - 2 * PAD;
  const sx = (e: number) => PAD + (t1 === t0 ? innerW / 2 : ((e - t0) / (t1 - t0)) * innerW);
  const sy = (v: number) => PAD + (vmax === vmin ? innerH / 2 : (1 - (v - vmin) / (vmax - vmin)) * innerH);

  const toggleBench = (symbol: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });

  // Map the cursor's screen-x into the viewBox domain (the SVG scales non-uniformly, so we
  // go through the wrapper's pixel width → viewBox x → epoch).
  function onMove(clientX: number) {
    const el = wrapRef.current;
    if (!el || !allEpochs.length) return;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return;
    const xView = ((clientX - rect.left) / rect.width) * VIEW_W;
    const frac = Math.min(1, Math.max(0, (xView - PAD) / innerW));
    setHoverE(t0 + frac * (t1 - t0));
  }

  // Snap the hover to the nearest actual sample so the cursor + dots sit on real points.
  const rows = hoverE != null ? hoverRows(lines, hoverE) : [];
  const snapE = rows.length ? epoch(rows[0]!.when) : null;
  const cursorX = snapE != null ? sx(snapE) : null;
  const leftPct = cursorX != null ? (cursorX / VIEW_W) * 100 : 0;
  const tooltipRight = leftPct > 60;

  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">Performance (indexed to 100)</span>
        <div className="flex items-center gap-2">
          {/* Benchmark toggles (feed-gated) */}
          {benchmarksAvailable && benchmarks.length > 0 ? (
            <div className="flex gap-1.5">
              {benchmarks.map((b) => {
                const on = !hidden.has(b.symbol);
                return (
                  <button
                    key={b.symbol}
                    type="button"
                    onClick={() => toggleBench(b.symbol)}
                    aria-pressed={on}
                    className={`rounded-full border px-2 py-0.5 text-[11px] transition ${
                      on ? "border-muted text-muted" : "border-line text-muted/50 hover:border-muted"
                    }`}
                  >
                    {b.symbol}
                  </button>
                );
              })}
            </div>
          ) : (
            <span className="text-[11px] text-muted/70">Benchmarks: Pro</span>
          )}
          {/* Time-range selector */}
          <div className="flex gap-1">
            {RANGES.map(([label], i) => (
              <button
                key={label}
                type="button"
                onClick={() => setRangeIdx(i)}
                aria-pressed={i === rangeIdx}
                className={`rounded-md border px-2 py-0.5 text-[11px] transition ${
                  i === rangeIdx ? "border-accent text-accent" : "border-line text-muted hover:border-muted"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {lines.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted">Not enough history yet for this range.</div>
      ) : (
        <>
          <div
            ref={wrapRef}
            className="relative"
            onMouseMove={(e) => onMove(e.clientX)}
            onMouseLeave={() => setHoverE(null)}
          >
            <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} preserveAspectRatio="none" className="h-48 w-full" role="img" aria-label="Account performance over time, indexed to 100">
              {/* 100 baseline */}
              <line x1={PAD} x2={VIEW_W - PAD} y1={sy(100)} y2={sy(100)} stroke="rgb(var(--c-line))" strokeWidth={1} strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
              {lines.map((l) => (
                <polyline
                  key={l.key}
                  points={l.points.map((p) => `${sx(epoch(p.when)).toFixed(1)},${sy(p.v).toFixed(1)}`).join(" ")}
                  fill="none"
                  stroke={l.color}
                  strokeWidth={2}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  strokeDasharray={l.dashed ? "5 3" : undefined}
                  vectorEffect="non-scaling-stroke"
                />
              ))}
              {/* Hover cursor + a dot on each line at the snapped sample. */}
              {cursorX != null ? (
                <>
                  <line x1={cursorX} x2={cursorX} y1={PAD} y2={VIEW_H - PAD} stroke="rgb(var(--c-line))" strokeWidth={1} vectorEffect="non-scaling-stroke" />
                  {lines.map((l) => {
                    const pt = snapE != null ? valueAt(l.points, snapE) : null;
                    return pt ? <circle key={l.key} cx={sx(epoch(pt.when))} cy={sy(pt.v)} r={2.5} fill={l.color} vectorEffect="non-scaling-stroke" /> : null;
                  })}
                </>
              ) : null}
            </svg>

            {/* Hover tooltip: each line's % return at the cursor, sorted high→low. */}
            {rows.length ? (
              <div
                className="pointer-events-none absolute top-1 z-10 min-w-[8rem] rounded-md border border-line bg-paper/95 p-2 text-[11px] shadow-lg"
                style={tooltipRight ? { right: `${100 - leftPct}%`, marginRight: "8px" } : { left: `${leftPct}%`, marginLeft: "8px" }}
              >
                <div className="mb-1 tabular-nums text-muted">{isoDate(rows[0]!.when)}</div>
                {rows.map((r) => (
                  <div key={r.key} className="flex items-center justify-between gap-3">
                    <span className="flex items-center gap-1.5">
                      <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: r.color }} />
                      <span className="text-muted">{r.label}</span>
                    </span>
                    <span className={`tabular-nums ${r.pct >= 0 ? "text-positive" : "text-negative"}`}>{fmtPct(r.pct)}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          <div className="mt-2 flex items-center justify-between text-[11px] tabular-nums text-muted">
            <span>{isoDate(new Date(t0).toISOString().slice(0, 10))}</span>
            <span>
              {vmin.toFixed(0)}–{vmax.toFixed(0)}
            </span>
            <span>{isoDate(new Date(t1).toISOString().slice(0, 10))}</span>
          </div>
          {/* Legend with each line's latest return over the visible range. */}
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-2 text-[11px]">
            {lines.map((l) => {
              const last = l.points[l.points.length - 1]!;
              const pct = last.v - 100;
              return (
                <span key={l.key} className="flex items-center gap-1.5">
                  <span className="inline-block h-0.5 w-4" style={{ backgroundColor: l.color, borderBottom: l.dashed ? `1px dashed ${l.color}` : undefined }} />
                  <span className="text-muted">{l.label}</span>
                  <span className={`tabular-nums ${pct >= 0 ? "text-positive" : "text-negative"}`}>{fmtPct(pct)}</span>
                </span>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
