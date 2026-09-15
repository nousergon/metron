"use client";

// Zone 2 — the settled NAV path. Tapping the chart cycles the period (§3g.4); the depth
// link goes straight to the page that computes the return series.

import { useMemo, useState } from "react";
import Link from "next/link";

import type { GlancePath as GlancePathZone } from "@/lib/api-glance";
import { ProvenanceBadge } from "@/components/glance-provenance";

const PERIODS = ["1M", "3M", "YTD", "1Y"] as const;
type Period = (typeof PERIODS)[number];

function startFor(period: Period, last: string): string {
  const d = new Date(`${last}T00:00:00Z`);
  if (period === "YTD") return `${d.getUTCFullYear()}-01-01`;
  const months = period === "1M" ? 1 : period === "3M" ? 3 : 12;
  d.setUTCMonth(d.getUTCMonth() - months);
  return d.toISOString().slice(0, 10);
}

export function GlancePath({ path, href }: { path: GlancePathZone; href: string }) {
  const [period, setPeriod] = useState<Period>("1M");
  const points = useMemo(() => {
    if (!path.points.length) return [];
    const from = startFor(period, path.points[path.points.length - 1].date);
    return path.points.filter((p) => p.date >= from);
  }, [path.points, period]);

  if (!path.available || points.length < 2) {
    return <p className="text-sm text-muted">{path.reason ?? "Not enough recorded history in this period yet."}</p>;
  }
  const navs = points.map((p) => p.nav);
  const lo = Math.min(...navs);
  const hi = Math.max(...navs);
  const span = hi - lo || 1;
  const d = points
    .map((p, i) => `${((i / (points.length - 1)) * 100).toFixed(2)},${(30 - ((p.nav - lo) / span) * 28 - 1).toFixed(2)}`)
    .join(" ");
  const up = navs[navs.length - 1] >= navs[0];

  return (
    <div>
      <button
        type="button"
        onClick={() => setPeriod(PERIODS[(PERIODS.indexOf(period) + 1) % PERIODS.length])}
        aria-label={`NAV path, ${period}. Tap to change period.`}
        className="block w-full"
      >
        <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-16 w-full" aria-hidden="true">
          <polyline
            points={d}
            fill="none"
            strokeWidth="1.2"
            vectorEffect="non-scaling-stroke"
            className={up ? "stroke-emerald-500" : "stroke-rose-500"}
          />
        </svg>
      </button>
      <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted">
        <span>
          {period} · tap to change
        </span>
        <span className="flex items-center gap-2">
          <ProvenanceBadge provenance={path.provenance} asOf={path.as_of} />
          <Link href={href} className="hover:text-ink">
            Performance →
          </Link>
        </span>
      </div>
    </div>
  );
}
