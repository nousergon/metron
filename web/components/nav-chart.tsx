// Portfolio NAV line chart (metron-ops#44) — a dependency-free inline SVG of the recorded
// NAV series under the Performance metrics. ABSOLUTE portfolio value only; the SPY/
// benchmark overlay is feed-gated (SP500 is copyrighted), so it arrives with the Pro
// market-data feed — not shown in the no-feed beta.

import { isoDate, moneyWhole } from "@/lib/format";

export type NavPoint = { snap_date: string; nav: number };

const VIEW_W = 600;
const VIEW_H = 160;
const PAD = 8;

/** Map a numeric series to SVG coords in [PAD, W-PAD] × [PAD, H-PAD], y inverted (higher
 *  value = higher on screen). A flat series sits on the vertical midline. Exported for
 *  testing the scaling in isolation. */
export function scaleSeries(
  values: number[],
  w = VIEW_W,
  h = VIEW_H,
  pad = PAD,
): { x: number; y: number }[] {
  const n = values.length;
  if (n === 0) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const innerW = w - 2 * pad;
  const innerH = h - 2 * pad;
  return values.map((v, i) => {
    const x = pad + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);
    const y = span === 0 ? pad + innerH / 2 : pad + (1 - (v - min) / span) * innerH;
    return { x, y };
  });
}

export function NavChart({ points, currency }: { points: NavPoint[]; currency: string }) {
  if (points.length < 2) return null; // a line needs two points

  const navs = points.map((p) => p.nav);
  const coords = scaleSeries(navs);
  const line = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  // Close an area under the line back along the baseline for a subtle fill.
  const area = `${line} ${coords[coords.length - 1]!.x.toFixed(1)},${VIEW_H - PAD} ${coords[0]!.x.toFixed(1)},${VIEW_H - PAD}`;

  const min = Math.min(...navs);
  const max = Math.max(...navs);

  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">Portfolio NAV</span>
        <span className="text-[11px] text-muted">SPY overlay arrives with the Pro feed</span>
      </div>
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        className="h-40 w-full"
        role="img"
        aria-label="Portfolio NAV over time"
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
        <span>
          {isoDate(points[0]!.snap_date)} · {moneyWhole(points[0]!.nav, currency)}
        </span>
        <span>
          range {moneyWhole(min, currency)}–{moneyWhole(max, currency)}
        </span>
        <span>
          {isoDate(points[points.length - 1]!.snap_date)} · {moneyWhole(points[points.length - 1]!.nav, currency)}
        </span>
      </div>
    </div>
  );
}
