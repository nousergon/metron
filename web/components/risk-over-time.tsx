// Rolling risk basket over time (metron-ops#67) — small multiples for the non-benchmark
// basket (Sharpe / Sortino / volatility / max drawdown / PSR / CVaR, metron-ops#190)
// computed over a trailing window. Dependency-free inline SVG sparklines, reusing the
// NavChart scaler. Benchmark-relative risk (alpha / beta / tracking error) stays Pro
// (SPY = feed-gated).

import type { RollingRiskPoint } from "@/lib/api";
import { isoDate } from "@/lib/format";
import { scaleSeries } from "@/components/nav-chart";
import { Section } from "@/components/ui";

const W = 260;
const H = 56;
const PAD = 4;

type MetricKey = "sharpe" | "sortino" | "volatility" | "max_drawdown" | "psr" | "cvar";
const METRICS: { key: MetricKey; label: string; fmt: (n: number) => string }[] = [
  { key: "sharpe", label: "Sharpe", fmt: (n) => n.toFixed(2) },
  { key: "sortino", label: "Sortino", fmt: (n) => n.toFixed(2) },
  { key: "volatility", label: "Volatility (ann.)", fmt: (n) => `${(n * 100).toFixed(1)}%` },
  { key: "max_drawdown", label: "Max drawdown", fmt: (n) => `${(n * 100).toFixed(1)}%` },
  { key: "psr", label: "PSR", fmt: (n) => `${(n * 100).toFixed(0)}%` },
  { key: "cvar", label: "CVaR (95%)", fmt: (n) => `${(n * 100).toFixed(1)}%` },
];

function Sparkline({ values }: { values: number[] }) {
  if (values.length < 2) {
    return <div className="flex h-14 items-center justify-center text-[11px] text-muted">building…</div>;
  }
  const coords = scaleSeries(values, W, H, PAD);
  const line = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-14 w-full" role="img" aria-hidden="true">
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

export function RiskOverTime({ rolling }: { rolling: RollingRiskPoint[] }) {
  if (rolling.length < 2) return null;
  const first = rolling[0]!;
  const last = rolling[rolling.length - 1]!;

  return (
    <Section
      title="Risk over time"
      note={`trailing ~3-month window · ${isoDate(first.snap_date)} → ${isoDate(last.snap_date)}`}
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {METRICS.map((m) => {
          const series = rolling.map((p) => p[m.key]).filter((v): v is number => v != null);
          const latest = last[m.key];
          return (
            <div key={m.key} className="rounded-lg border border-line p-3">
              <div className="flex items-baseline justify-between">
                <span className="text-xs uppercase tracking-wide text-muted">{m.label}</span>
                <span className="text-sm font-semibold tabular-nums">{latest != null ? m.fmt(latest) : "—"}</span>
              </div>
              <div className="mt-2">
                <Sparkline values={series} />
              </div>
            </div>
          );
        })}
      </div>
      <p className="mt-2 text-[11px] text-muted">
        Sharpe, Sortino, volatility, max drawdown, Probabilistic Sharpe Ratio (PSR — probability the true Sharpe
        exceeds zero, given how much history exists) and CVaR (95% — average loss in the worst 5% of periods),
        computed the same way for every Metron portfolio. PSR needs ≥30 return observations before it appears.
        Benchmark-relative risk (alpha / beta / tracking error) arrives with the Pro market-data feed.
      </p>
    </Section>
  );
}
