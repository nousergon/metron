// Per-indicator macro card for the Macro detail page: latest value + change + a
// dependency-free inline-SVG line of the last ~12 months (reuses scaleSeries from the
// NAV chart). FRED history arrives most-recent-first; we reverse to ascending and clip
// to a ~1-year window. No fabrication — a series with too little history just shows
// fewer points (or its value only).

import type { MacroIndicator, MacroPoint } from "@/lib/api";
import { isoDate, signClass } from "@/lib/format";
import { scaleSeries } from "@/components/nav-chart";

const VIEW_W = 600;
const VIEW_H = 120;
const PAD = 8;
const ONE_YEAR_MS = 365 * 24 * 60 * 60 * 1000;

function fmtValue(units: string, v: number): string {
  return units === "%" ? `${v.toFixed(2)}%` : v.toFixed(2);
}

function fmtChange(units: string, c: number | null): string {
  if (c == null) return "—";
  const sign = c > 0 ? "+" : c < 0 ? "−" : "";
  const mag = Math.abs(c).toFixed(2);
  return units === "%" ? `${sign}${mag} pp` : `${sign}${mag}`;
}

/** Last ~12 months of the series, ascending. Falls back to whatever history exists when
 *  fewer than two points land inside the window. */
function lastYear(history: MacroPoint[], latestDate: string): MacroPoint[] {
  const asc = [...history].reverse(); // API gives most-recent-first
  const cutoff = new Date(latestDate).getTime() - ONE_YEAR_MS;
  const windowed = asc.filter((p) => new Date(p.obs_date).getTime() >= cutoff);
  return windowed.length >= 2 ? windowed : asc;
}

export function MacroChart({ ind }: { ind: MacroIndicator }) {
  const series = lastYear(ind.history, ind.latest_date);
  const values = series.map((p) => p.value);
  const hasLine = values.length >= 2;
  const coords = hasLine ? scaleSeries(values, VIEW_W, VIEW_H, PAD) : [];
  const line = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const area = hasLine
    ? `${line} ${coords[coords.length - 1]!.x.toFixed(1)},${VIEW_H - PAD} ${coords[0]!.x.toFixed(1)},${VIEW_H - PAD}`
    : "";
  const min = hasLine ? Math.min(...values) : null;
  const max = hasLine ? Math.max(...values) : null;

  return (
    // scroll-mt offsets the in-page #anchor jump from the macro-strip tiles below the nav.
    <div id={ind.key} className="scroll-mt-24 rounded-lg border border-line bg-surface p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-sm font-medium" title={ind.label}>
          {ind.label}
        </span>
        <span className="shrink-0 text-[11px] tabular-nums text-muted" title={`Last updated ${isoDate(ind.latest_date)}`}>
          {isoDate(ind.latest_date)}
        </span>
      </div>
      <div className="mt-0.5 flex items-baseline gap-2">
        <span className="text-xl font-semibold tabular-nums">{fmtValue(ind.units, ind.latest_value)}</span>
        <span className={`text-xs tabular-nums ${signClass(ind.change ?? 0)}`}>{fmtChange(ind.units, ind.change)}</span>
      </div>

      {hasLine ? (
        <>
          <svg
            viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
            preserveAspectRatio="none"
            className="mt-3 h-28 w-full"
            role="img"
            aria-label={`${ind.label} — last 12 months`}
          >
            <polygon points={area} fill="rgb(var(--c-accent) / 0.10)" stroke="none" />
            <polyline
              points={line}
              fill="none"
              stroke="rgb(var(--c-accent))"
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <div className="mt-2 flex justify-between text-[11px] tabular-nums text-muted">
            <span>{isoDate(series[0]!.obs_date)}</span>
            <span>
              range {fmtValue(ind.units, min as number)}–{fmtValue(ind.units, max as number)}
            </span>
            <span>{isoDate(series[series.length - 1]!.obs_date)}</span>
          </div>
        </>
      ) : (
        <p className="mt-3 text-[11px] text-muted">Not enough history yet for a chart.</p>
      )}
    </div>
  );
}
