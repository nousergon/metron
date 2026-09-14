"use client";

// Technical rating track record (metron-ops#298, Brian ruling 2026-09-14) — the
// Diagnostics card's interactive view of the rating's realized near-term forward
// performance. Owner (feed-entitled) build only, same gate as the rating itself.
//
// MEASURED 2026-09-14 (backfill grade): the rating carries NO predictive edge at 1-20 days
// (IC ~= -0.017 at 5d). This card is deliberately NEUTRAL — every IC is shown next to its
// own noise_floor_ic, and nothing here is phrased as a recommendation.

import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { RatingPerformance } from "@/lib/api";
import { accountingPercent, isoDate, percent } from "@/lib/format";
import { Section, Table } from "@/components/ui";
import { scaleSeries } from "@/components/nav-chart";

const LABEL_ORDER = ["Strong Sell", "Sell", "Neutral", "Buy", "Strong Buy"] as const;

const SEGMENT_OPTIONS: { key: string; label: string }[] = [
  { key: "all", label: "All history" },
  { key: "live", label: "Live (out-of-sample)" },
  { key: "backfill", label: "Backfill (simulated)" },
];

function fmtPct(v: number | null): string {
  return v != null ? accountingPercent(v) : "—";
}

function fmtRate(v: number | null): string {
  return v != null ? percent(v) : "—";
}

function fmtIc(v: number | null): string {
  return v != null ? v.toFixed(3) : "—";
}

const W = 260;
const H = 48;
const PAD = 4;

function IcSparkline({ values }: { values: number[] }) {
  if (values.length < 2) {
    return <div className="flex h-12 items-center justify-center text-[11px] text-muted">not enough history yet</div>;
  }
  const coords = scaleSeries(values, W, H, PAD);
  const line = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  // Zero line, since IC is signed and "near zero" is the honest expectation here — same
  // min/max scaling scaleSeries uses internally, computed here so 0 needn't be a data point.
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const innerH = H - 2 * PAD;
  const zeroY = span === 0 ? PAD + innerH / 2 : PAD + (1 - (0 - min) / span) * innerH;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-12 w-full" role="img" aria-hidden="true">
      {zeroY != null ? (
        <line x1={0} y1={zeroY} x2={W} y2={zeroY} stroke="currentColor" strokeOpacity={0.15} strokeWidth={1} />
      ) : null}
      <polyline
        points={line}
        fill="none"
        stroke="rgb(var(--c-accent))"
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={`rounded-full border px-2.5 py-0.5 text-xs transition ${
        on ? "border-accent/40 bg-accent/10 text-ink" : "border-line text-muted/70 hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

export function RatingTrackRecordCard({ rp }: { rp: RatingPerformance }) {
  const defaultHorizon = rp.horizons.includes(5) ? 5 : (rp.horizons[0] ?? 5);
  const defaultWindow = rp.windows.includes(60) ? 60 : (rp.windows[0] ?? 60);
  const [horizon, setHorizon] = useState(defaultHorizon);
  const [windowN, setWindowN] = useState(defaultWindow);
  const [segment, setSegment] = useState(rp.segments["all"] ? "all" : Object.keys(rp.segments)[0] || "all");

  const stats = rp.segments[segment]?.[String(windowN)]?.[String(horizon)];

  const icHistory = useMemo(
    () =>
      rp.ic_series
        .filter((p) => p.horizon === horizon && p.ic != null)
        .sort((a, b) => a.date.localeCompare(b.date))
        .map((p) => p.ic as number),
    [rp.ic_series, horizon],
  );

  const availableSegments = SEGMENT_OPTIONS.filter((s) => rp.segments[s.key]);

  return (
    <Section
      title="Technical rating track record"
      note={rp.as_of_utc ? `as of ${isoDate(rp.as_of_utc.slice(0, 10))}` : undefined}
    >
      <p className="text-xs text-muted">
        Realized forward performance of the technical rating, measured — not a recommendation. The Backfill segment
        is simulated on today&apos;s universe (survivorship bias); only Live is out-of-sample.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted">Horizon</span>
          {rp.horizons.map((h) => (
            <Chip key={h} on={h === horizon} onClick={() => setHorizon(h)}>
              {h}d
            </Chip>
          ))}
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-muted">Window</span>
          {rp.windows.map((w) => (
            <Chip key={w} on={w === windowN} onClick={() => setWindowN(w)}>
              {w} sessions
            </Chip>
          ))}
        </div>
        {availableSegments.length > 1 ? (
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wide text-muted">Segment</span>
            {availableSegments.map((s) => (
              <Chip key={s.key} on={s.key === segment} onClick={() => setSegment(s.key)}>
                {s.label}
              </Chip>
            ))}
          </div>
        ) : null}
      </div>

      {stats ? (
        <>
          <div className="mt-4">
            <Table head={["Rating", "N", "Avg forward return", "Excess vs universe", "Hit rate"]}>
              {LABEL_ORDER.filter((l) => stats.buckets[l]).map((label) => {
                const b = stats.buckets[label]!;
                return (
                  <tr key={label} className="border-b border-line last:border-0">
                    <td className="px-4 py-2 font-medium">{label}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{b.n ?? "—"}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{fmtPct(b.mean_fwd)}</td>
                    <td
                      className={`px-4 py-2 text-right tabular-nums ${
                        b.mean_excess != null && b.mean_excess !== 0 ? (b.mean_excess > 0 ? "text-positive" : "text-negative") : "text-muted"
                      }`}
                    >
                      {fmtPct(b.mean_excess)}
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums text-muted">{fmtRate(b.hit_rate)}</td>
                  </tr>
                );
              })}
            </Table>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div className="rounded-lg border border-line p-3">
              <div className="text-xs uppercase tracking-wide text-muted">Strong Buy − Strong Sell spread</div>
              <div className="mt-1 text-lg font-semibold tabular-nums">
                {fmtPct(stats.spread_strong_buy_minus_strong_sell)}
              </div>
            </div>
            <div className="rounded-lg border border-line p-3 sm:col-span-2">
              <div className="flex items-baseline justify-between">
                <span className="text-xs uppercase tracking-wide text-muted">IC vs noise floor</span>
                <span className="text-xs text-muted">{stats.ic_n_dates != null ? `${stats.ic_n_dates} dates` : undefined}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-3">
                <span className="text-lg font-semibold tabular-nums">{fmtIc(stats.ic_mean)}</span>
                <span className="text-xs text-muted">noise floor {fmtIc(stats.noise_floor_ic)}</span>
              </div>
              <div className="mt-2">
                <IcSparkline values={icHistory} />
              </div>
              <p className="mt-1 text-[11px] text-muted">IC over time, {horizon}d horizon.</p>
            </div>
          </div>
        </>
      ) : (
        <div className="mt-4 rounded-lg border border-dashed border-line p-6 text-sm text-muted">
          {segment === "live" ? "No live data yet — Live starts accruing once the rating has run out-of-sample." : "Not yet published for this window/horizon."}
        </div>
      )}
    </Section>
  );
}
