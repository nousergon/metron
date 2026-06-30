"use client";

// Allocation breakdown for the Holdings page: the US-vs-international split (from each
// holding's country of domicile) and a by-sector weighting. Both are by base-currency
// market value over non-cash holdings, computed client-side from the loaded holdings.
// Cash is excluded (no geography/sector); an unclassified holding is surfaced honestly
// rather than folded into a guessed bucket.

import { useMemo } from "react";
import type { Holding } from "@/lib/api";
import { accountingPercent, moneyWhole } from "@/lib/format";

const US_COUNTRY = "United States";
// Sentinel for a multi-country / ex-US holding (a broad-international fund whose listing
// domicile doesn't describe its exposure). Mirrors api/services/countries.py INTERNATIONAL;
// set via a tenant classification override, it buckets International like any foreign domicile.
const INTERNATIONAL = "International";

function geoBucket(country: string | null): "US" | "International" | "Unclassified" {
  if (country == null) return "Unclassified";
  if (country === INTERNATIONAL) return "International";
  return country === US_COUNTRY ? "US" : "International";
}

function StackBar({ segments }: { segments: { label: string; value: number; className: string }[] }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  if (total <= 0) return null;
  return (
    <div className="flex h-3 w-full overflow-hidden rounded-sm">
      {segments.map((s) =>
        s.value > 0 ? (
          <div key={s.label} className={s.className} style={{ width: `${(s.value / total) * 100}%` }} title={`${s.label}: ${accountingPercent(s.value / total)}`} />
        ) : null,
      )}
    </div>
  );
}

export function AllocationBreakdown({ holdings, baseCurrency }: { holdings: Holding[]; baseCurrency: string }) {
  const { geo, sectors, classifiedTotal, valuedTotal } = useMemo(() => {
    const geo = { US: 0, International: 0, Unclassified: 0 };
    const sectorMap = new Map<string, number>();
    let valuedTotal = 0;
    for (const h of holdings) {
      if (h.security_type === "cash") continue;
      const mv = h.market_value;
      if (mv == null || mv <= 0) continue; // can't weight an unpriced/foreign-no-FX holding
      valuedTotal += mv;
      geo[geoBucket(h.country)] += mv;
      const sec = h.sector ?? "Unclassified";
      sectorMap.set(sec, (sectorMap.get(sec) ?? 0) + mv);
    }
    const classifiedTotal = geo.US + geo.International;
    const sectors = [...sectorMap.entries()].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
    return { geo, sectors, classifiedTotal, valuedTotal };
  }, [holdings]);

  if (valuedTotal <= 0) {
    return <p className="text-xs text-muted">No priced non-cash holdings to allocate. Refresh prices to see the breakdown.</p>;
  }

  const usPct = classifiedTotal > 0 ? geo.US / classifiedTotal : null;
  const intlPct = classifiedTotal > 0 ? geo.International / classifiedTotal : null;
  const maxSector = sectors.reduce((m, s) => Math.max(m, s.value), 0);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {/* US vs International (by country of domicile) */}
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">US vs International</div>
        {classifiedTotal > 0 ? (
          <>
            <StackBar
              segments={[
                { label: "US", value: geo.US, className: "bg-accent" },
                { label: "International", value: geo.International, className: "bg-positive/70" },
              ]}
            />
            <div className="mt-3 space-y-1 text-xs">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full bg-accent" /> US
                </span>
                <span className="tabular-nums">
                  {usPct != null ? accountingPercent(usPct) : "—"} <span className="text-muted">· {moneyWhole(geo.US, baseCurrency)}</span>
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-1.5">
                  <span className="inline-block h-2 w-2 rounded-full bg-positive/70" /> International
                </span>
                <span className="tabular-nums">
                  {intlPct != null ? accountingPercent(intlPct) : "—"} <span className="text-muted">· {moneyWhole(geo.International, baseCurrency)}</span>
                </span>
              </div>
            </div>
          </>
        ) : (
          <p className="text-xs text-muted">No holdings have a resolved country yet.</p>
        )}
        {geo.Unclassified > 0 ? (
          <p className="mt-2 text-[11px] text-muted">
            {moneyWhole(geo.Unclassified, baseCurrency)} ({accountingPercent(geo.Unclassified / valuedTotal)} of priced) has no
            resolved country — excluded from the split above.
          </p>
        ) : null}
        <p className="mt-1 text-[11px] text-muted">By country of domicile. A fund/ETF defaults to its listing domicile — not its underlying exposure; reclassify a broad-international fund as &ldquo;International&rdquo; via its Country override.</p>
      </div>

      {/* By sector */}
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">By sector</div>
        <ul className="space-y-1.5">
          {sectors.map((s) => (
            <li key={s.label} className="flex items-center gap-2 text-xs">
              <span className="w-28 shrink-0 truncate text-muted" title={s.label}>
                {s.label}
              </span>
              <div className="flex-1">
                <div className="h-3 rounded-sm bg-accent/60" style={{ width: `${maxSector > 0 ? Math.max(2, (s.value / maxSector) * 100) : 0}%` }} />
              </div>
              <span className="w-12 shrink-0 text-right tabular-nums">{accountingPercent(s.value / valuedTotal)}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[11px] text-muted">Share of priced non-cash market value.</p>
      </div>
    </div>
  );
}
