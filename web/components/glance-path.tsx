"use client";

// Zone 2 — the settled NAV path when closed, the intraday sparkline while open
// (positioning §3g.4, metron-ops-I324). Tapping the settled chart cycles the period; the
// depth link goes straight to the page that computes the return series. The open-state
// sparkline has only one period ("today") so it renders without the period control —
// tapping it instead reveals the settled history underneath, never mixing the two
// point arrays into one line.

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

/** A polyline `points` attribute for any (x-key, nav) series, scaled into the shared
 *  16:h viewBox. Exported for testing the geometry in isolation. */
export function polylinePoints(navs: number[]): string {
  if (navs.length < 2) return "";
  const lo = Math.min(...navs);
  const hi = Math.max(...navs);
  const span = hi - lo || 1;
  return navs
    .map((nav, i) => `${((i / (navs.length - 1)) * 100).toFixed(2)},${(30 - ((nav - lo) / span) * 28 - 1).toFixed(2)}`)
    .join(" ");
}

function Sparkline({ d, up, label }: { d: string; up: boolean; label: string }) {
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="h-16 w-full" role="img" aria-label={label}>
      <polyline
        points={d}
        fill="none"
        strokeWidth="1.2"
        vectorEffect="non-scaling-stroke"
        className={up ? "stroke-emerald-500" : "stroke-rose-500"}
      />
    </svg>
  );
}

export function GlancePath({ path, href }: { path: GlancePathZone; href: string }) {
  const [period, setPeriod] = useState<Period>("1M");
  // Open state, live series reachable (metron-ops-I324): show TODAY's intraday
  // sparkline by default, distinct from — never merged with — the settled history.
  const [showSettled, setShowSettled] = useState(false);
  const liveAvailable = path.state === "open" && path.intraday_points.length >= 2;

  const points = useMemo(() => {
    if (!path.points.length) return [];
    const from = startFor(period, path.points[path.points.length - 1].date);
    return path.points.filter((p) => p.date >= from);
  }, [path.points, period]);

  if (liveAvailable && !showSettled) {
    const navs = path.intraday_points.map((p) => p.nav);
    const up = navs[navs.length - 1]! >= navs[0]!;
    return (
      <div>
        <button
          type="button"
          onClick={() => setShowSettled(true)}
          aria-label="Today's intraday NAV path. Tap to see recent history instead."
          className="block w-full"
        >
          <Sparkline d={polylinePoints(navs)} up={up} label="Today's intraday NAV path" />
        </button>
        <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted">
          <span>Today · tap for history</span>
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

  if (!path.available || points.length < 2) {
    return <p className="text-sm text-muted">{path.reason ?? "Not enough recorded history in this period yet."}</p>;
  }
  const navs = points.map((p) => p.nav);
  const up = navs[navs.length - 1]! >= navs[0]!;

  return (
    <div>
      <button
        type="button"
        onClick={() => (liveAvailable ? setShowSettled(false) : setPeriod(PERIODS[(PERIODS.indexOf(period) + 1) % PERIODS.length]))}
        aria-label={liveAvailable ? "Tap to return to today's live path." : `NAV path, ${period}. Tap to change period.`}
        className="block w-full"
      >
        <Sparkline d={polylinePoints(navs)} up={up} label="NAV path" />
      </button>
      <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted">
        <span>{liveAvailable ? "History · tap for today" : `${period} · tap to change`}</span>
        <span className="flex items-center gap-2">
          {path.state === "open" && !liveAvailable && path.reason ? (
            <span className="text-muted/70">{path.reason}</span>
          ) : null}
          <ProvenanceBadge provenance={path.provenance} asOf={path.as_of} />
          <Link href={href} className="hover:text-ink">
            Performance →
          </Link>
        </span>
      </div>
    </div>
  );
}
